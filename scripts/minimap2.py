"""
Adapted from https://github.com/PoonLab/covizu/covizu/minimap2.py
"""

import subprocess
import argparse
import re
import sys
import os
import json

from seq_utils import SC2Locator


def minimap2(fq1, fq2, ref, path='minimap2', nthread=3, report=1e5):
    """
    Wrapper for minimap2 for processing paired-end read data.
    :param fq1:  str, path to FASTQ R1 file (uncompressed or gzipped)
    :param fq2:  str, path to FASTQ R2 file (uncompressed or gzipped); if None,
                 treat fq1 as an unpaired (single) FASTQ
    :param ref:  str, path to FASTA file with reference sequence
    :param path:  str, path to minimap2 binary executable
    :param nthread:  int, number of threads to run
    :param report:  int, reporting frequency (to stderr)
    :yield:  tuple (qname, rpos, cigar, seq)
    """
    if fq2 is None:
        # assume we are working with Oxford Nanopore (ONT)
        p = subprocess.Popen(
            [path, '-t', str(nthread), '-ax', 'map-ont', '--eqx', '--secondary=no', ref, fq1],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    else:
        # assume we are working with paired-end Illumina
        p = subprocess.Popen(
            [path, '-t', str(nthread), '-ax', 'sr', '--eqx', '--secondary=no', ref, fq1, fq2],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    bad = 0
    output = map(lambda x: x.decode('utf-8'), p.stdout)
    for ln, line in enumerate(output):
        if ln % report == 0 and ln > 0:
            sys.stderr.write("{} reads, {} ({}%) mapped\n".format(
                ln/(2 if fq2 else 1), (ln-bad)/(2 if fq2 else 1), round((ln-bad)/ln*100)))
            sys.stderr.flush()

        if line == '' or line.startswith('@'):
            # split on \n leaves empty line; @ prefix header lines
            continue

        qname, flag, rname, rpos, _, cigar, _, _, _, seq, qual = \
            line.strip('\n').split('\t')[:11]

        if rname == '*' or ((int(flag) & 0x800) != 0):
            # did not map, or supplementary alignment
            bad += 1
            continue

        rpos = int(rpos) - 1  # convert to 0-index
        yield qname, rpos, cigar, seq, qual


def matchmaker(samfile):
    """
    An iterator that returns pairs of reads sharing a common qname from a SAM file.
    Note that unpaired reads will be left in the cached_rows dictionary and
    discarded.
    FIXME: it would probably be simpler to just pair up rows after headers
    @param samfile: open file handle to a SAM file
    @return: yields a tuple for each read pair with fields split by tab chars:
        ([qname, flag, rname, ...], [qname, flag, rname, ...])
    """
    cached_rows = {}
    for row in samfile:
        qname = row[0]
        old_row = cached_rows.pop(qname, None)
        if old_row is None:
            cached_rows[qname] = row
        else:
            # current row should be the second read of the pair
            yield old_row, row


def encode_diffs(row, reflen=29903, alphabet='ACGT', minq=10):
    """
    Serialize differences of query sequences to reference
    genome, which comprise nucleotide substitutions, in-frame
    indels, and locations of missing data.
    NOTE: runs of 'N's are also represented by 'X' tokens in the CIGAR
    string.
    :param row:  tuple, from minimap2(), i.e., (qname, rpos, cigar, seq)
    :param reflen:  int, length of reference genome
    :param alphabet:  str, accepted character states (nucleotides)
    :param minq:  int, minimum base quality
    """
    qname, rpos, cigar, seq, qual = row  # unpack tuple
    diffs = []
    missing = []
    if rpos > 0:
        # incomplete on left
        missing.append(tuple([0, rpos]))
    left = 0  # index for query

    tokens = re.findall(r'  (\d+)([MIDNSHPX=])', cigar, re.VERBOSE)
    for length, operator in tokens:
        length = int(length)
        substr = seq[left:(left + length)]
        subqual = qual[left:(left + length)]

        if operator == 'X':
            # base mismatches
            if 'N' in substr:
                # for now, assume the whole substring is bs
                missing.append(tuple([rpos, rpos+length]))
            else:
                # assume adjacent mismatches are independent substitutions
                for i, nt in enumerate(substr):
                    q = ord(subqual[i]) - 33
                    if nt in alphabet and q >= minq:
                        diffs.append(tuple(['~', rpos + i, nt]))
                    else:
                        # skip ambiguous base calls, like "R"
                        missing.append(tuple([rpos+i, rpos+i+1]))
            left += length
            rpos += length

        elif operator == 'S':
            # discard soft clip
            left += length

        elif operator == 'I':
            # insertion relative to reference
            diffs.append(tuple(['+', rpos, substr]))
            left += length

        elif operator == 'D':
            # deletion relative to reference
            diffs.append(tuple(['-', rpos, length]))
            rpos += length

        elif operator == '=':
            # exact match
            left += length
            rpos += length

        elif operator == 'H':
            # hard clip, do nothing
            pass

        else:
            print("ERROR: unexpected operator {}".format(operator))
            sys.exit()

    # update missing if sequence incomplete on the right
    if rpos < reflen:
        missing.append(tuple([rpos, reflen]))

    return qname, diffs, missing


def check_miss(miss):
    """ Restore missing lower or upper bounds for missing ranges """
    if len(miss) < 2:
        if miss[0][0] == 0:
            # missing right bound
            miss.append(tuple([29903, 29903]))
        elif miss[0][1] == 29903:
            # missing left bound
            miss.insert(0, tuple([0, 0]))
        else:
            print("Unexpected case in check_miss(): {}".format(miss))
            sys.exit()
    return miss


def merge_diffs(diff1, diff2, miss1, miss2):
    """
    Merge the differences from reference for paired reads (including coverage).

    If diff is on the overlap of coverage, the diff must appear in both.
    If diff exists where only one read has coverage, always accept.
    If diff is only in one read and both have coverage, reject.
    """
    miss1 = check_miss(miss1)
    miss2 = check_miss(miss2)

    lo1 = miss1[0][1]  # miss1 is [(0, lo1), (hi1, 29903)]
    hi1 = miss1[1][0]
    lo2 = miss2[0][1]
    hi2 = miss2[1][0]

    if miss1 == miss2:
        # same coverage: only return duplicates
        same_diffs = [value for value in diff1 if value in diff2]
        return same_diffs, [(lo1, hi1)]
    elif lo1 > hi2 or lo2 > hi1:
        # non-overlapping, return all mutations
        all_diffs = list(set(diff1 + diff2))
        return all_diffs, [(lo1, hi1), (lo2, hi2)]
    else:
        # imperfect overlap
        cov_union = (min([lo1, lo2]), max([hi1, hi2]))
        cov_intersect = (max([lo1, lo2]), min([hi1, hi2]))

        all_diffs = list(set(diff1 + diff2))
        return_diffs = [value for value in diff1 if value in diff2]
        diff_diffs = [value for value in all_diffs if value not in return_diffs]

        diff_loc = [value for value in map(lambda x: x[1], diff_diffs)]
        for i in range(len(diff_diffs)):
            if cov_union[0] <= diff_loc[i] <= cov_union[1] and not cov_intersect[0] <= diff_loc[i] <= cov_intersect[1]:
                return_diffs.append(diff_diffs[i])
        return return_diffs, [cov_union]


def parse_mm2(mm2, locator, paired=True, stop=1e6, maxpos=29903):
    """
    Iterate over minimap2 output
    :param mm2:  generator, yields tuples of SAM output from minimap2
    :param locator:  SC2locator object, maps mutations to nicer notation
    :param paired:  logical, if False, treat as single-end reads
    :param stop:  int, limit to number of paired reads that mapped to sc2 to report
    :param maxpos:  int, genome length
    :return:  list, features for every pair of reads
              dict, coverage per nucleotide position
    """
    count = 0
    total_coverage = dict([(pos, 0) for pos in range(maxpos)])
    res = []
    iter = matchmaker(mm2) if paired else mm2

    for row in iter:
        if paired:
            r1, r2 = row
            _, diff1, miss1 = encode_diffs(r1)
            _, diff2, miss2 = encode_diffs(r2)
            diffs, coverage = merge_diffs(diff1, diff2, miss1, miss2)
        else:
            _, diffs, miss = encode_diffs(row)
            coverage = []
            for i in range(0, len(miss)-1):
                _, left = miss[i]
                right, _ = miss[i+1]
                if left == right:
                    continue  # adjacent missing intervals
                coverage.append(tuple([left, right]))

        for left, right in coverage:
            for pos in range(left, right):
                total_coverage[pos] += 1

        # get indel or AA sub notations
        mut = [locator.parse_mutation(d) for d in diffs]
        res.append({"diff": diffs, "mutations": mut})

        count += 1
        if stop and count > stop:
            break

    return res, total_coverage


def get_frequencies(res, coverage):
    """
    Normalize mutation counts by position-specific read depth (coverage)
    :param res:  list, mutations for each read/pair
    :param coverage:  dict, read depth per reference position
    :return:  dict, relative frequency for every mutation
    """
    counts = {}
    for row in res:
        for i, diff in enumerate(row['diff']):
            typ, pos, mut = diff
            label = row['mutations'][i]
            denom = coverage[pos]
            if denom == 0:
                continue
            
            if typ == '~':
                key = ''.join(map(str, [typ, pos+1, mut]))
            else:
                key = ''.join(map(str, [typ, pos+1, ".", mut]))

            if pos not in counts:
                counts.update({pos: {}})
            if key not in counts[pos]:
                counts[pos].update({key: {'label': label, 'count': 0}})
            try:
                counts[pos][key]['count'] += 1/denom
            except ZeroDivisionError:
                print(pos, row, coverage[pos])
                raise
    return counts


def parse_args():
    parser = argparse.ArgumentParser("Wrapper script for minimap2")

    parser.add_argument('fq1', type=str, help="<input> path to FASTQ, or R1 if paired file.  "
                                                "May be gzip'ed.")
    parser.add_argument('fq2', type=str, nargs='?',
                        help="<input> path to FASTQ R2 file if paired.  May be gzip'ed.")
    parser.add_argument('-o', '--outdir', type=str, help="<output> directory to write files",
                        default=os.getcwd())
    parser.add_argument('-p', '--prefix', type=str, help="<output> prefix for output files")

    parser.add_argument('--limit', type=int, default=None,
                        help="Maximum number of reads to process (for debugging); default process all.")

    parser.add_argument('--path', type=str, default='minimap2',
                        help="<option> path to minimap2 executable")
    parser.add_argument('-a', '--align', action='store_true',
                        help="<option> output aligned sequences as FASTA")
    parser.add_argument('-t', '--thread', type=int, default=3,
                        help="<option> number of threads")

    parser.add_argument('--filter', action='store_true',
                        help="<option> filter problematic sites")

    ref_file = os.path.join("data", "NC_045512.fa")
    vcf_file = os.path.join("data", "problematic_sites_sarsCov2.vcf")

    parser.add_argument('--ref', type=str, default=ref_file,
                        help="<input> path to target FASTA (reference)")
    parser.add_argument("--vcf", type=str, default=vcf_file,
                        help="Path to VCF file of problematic sites in SARS-COV-2 genome. "
                             "Source: https://github.com/W-L/ProblematicSites_SARS-CoV2")

    return parser.parse_args()


def make_filename(outdir, prefix, suffix):
    """ TODO: handle placement of dots """
    filename = os.path.join(outdir, "{}.{}}".format(prefix, suffix))
    if os.path.exists(filename):
        print("Output file {} already exists, use --replace to overwrite")
        sys.exit()
    return filename


if __name__ == '__main__':
    args = parse_args()

    mm2 = minimap2(args.fq1, args.fq2, ref=args.ref, nthread=args.thread, path=args.path)
    locator = SC2Locator(ref_file='data/NC_045512.fa')
    res, coverage = parse_mm2(mm2, locator, paired=args.fq2 is not None, stop=args.limit)
    counts = get_frequencies(res, coverage)

    outfile = open(make_filename(args.outdir, args.prefix, ".mapped.csv"), 'w')
    outfile.write("position,label,mutation,frequency,coverage\n")
    for pos, mutations in counts.items():
        denom = coverage[pos]
        for mut, freq in mutations.items():
            outfile.write('{},{},{},{},{}\n'.format(
                pos, mut, freq['label'], freq['count'], denom))
    outfile.close()

    with open(make_filename(args.outdir, args.prefix, ".coverage.csv"), 'w') as covfile:
        covfile.write('position,coverage\n')
        for pos, count in coverage.items():
            covfile.write('{},{}\n'.format(pos, count))
