from datetime import date, datetime
from progress_utils import Callback
from csv import DictWriter, DictReader
import sqlite3
import argparse
import os
import glob
import subprocess
import sys
import tempfile
import requests
import pandas as pd
import json

from trim import send_error_notification, rename_file
from generate_summary import LinParser


def open_connection(database, callback=None):
    """
    Open connection to the database. Creates db file if it doesn't exist

    :param database: str, name of the database
    :param callback: function, option to print messages to the console
    :return: objects, connection object and cursor object
    """
    if not os.path.exists(database):
        if callback:
            callback("Database doesn't exist. Creating {}".format(database))

    conn = sqlite3.connect(database, check_same_thread=False)
    cur = conn.cursor()

    # create table if it don't exist
    records_table = 'CREATE TABLE IF NOT EXISTS RECORDS (file VARCHAR(255) ' \
                    'PRIMARY KEY, creation_date DATE, location VARCHAR(255), checksum VARCHAR(255));'
    cur.execute(records_table)

    conn.commit()
    return cur, conn


def insert_record(curr, filepath):
    """
    Inserts the file name, file creation date, path and checksum of the file into the database

    :param curr: object, cursor object
    :param filepath: str, path to the file
    :return: None
    """
    path, _ = os.path.split(filepath)

    # Get creation date
    file_timestamp = os.path.getmtime(filepath)
    date_obj_r1 = datetime.fromtimestamp(file_timestamp)
    formatted_date = date_obj_r1.strftime('%Y-%m-%d %H:%M:%S')

    # Calculate SHA-1 Checksum
    stdout = subprocess.getoutput("sha1sum {}".format(filepath))
    checksum = stdout.split(' ')[0]

    # Insert into Database
    curr.execute("INSERT OR REPLACE INTO RECORDS (file, creation_date, location, checksum)"
                 " VALUES(?, ?, ?, ?)",
                 [filepath, formatted_date, path, checksum])


def get_runs(paths, ignore_list, runs_list, callback=None):
    """
    Gets the list of runs to process

    :param paths: list, paths to all R1 files in the uploads directory
    :param ignore_list: list, directories that the user would like to avoid processing
    :param runs_list: list, directories that the user would like to processing, processes everything if none specified
    :param callback: function, option to print messages to the console
    :return: list, paths to all files that have not been inserted into the database and the filepaths to all runs
    """
    runs = set()
    ignored = set()
    for file in paths:
        path, filename = os.path.split(file)
        if contains_run(path, ignore_list):
            ignored.add(path)
            continue
        if len(runs_list) == 0 or contains_run(path, runs_list):
            runs.add(path)

    for path in ignored:
        callback("Ignoring '{}'".format(path))

    return runs


def get_files(curr, paths, ignore_list, check_processed, callback=None):
    """
    Detects if there are any new data files that have been uploaded by comparing the list of files to those that
    are already in the database

    :param curr: object, cursor object
    :param paths: list, paths to all R1 files in the uploads directory
    :param ignore_list: list, directories that the user would like to avoid processing
    :param callback: function, option to print messages to the console
    :return: list, paths to all files that have not been inserted into the database and the filepaths to all runs
    """
    runs = set()
    curr.execute("SELECT file, checksum FROM RECORDS;")
    results = {x[0]: x[1] for x in curr.fetchall()}

    unentered = []
    entered = []
    ignore = []
    for file in paths:
        if contains_run(file, ignore_list):
            ignore.append(file)
            continue
        path, filename = os.path.split(file)
        if file not in results:
            unentered.append(file)
            runs.add(path)
        else:
            entered.append(file)

    if check_processed:
        # Check if R1 or R2 files have changed since the last run
        if len(entered) > 0:
            r2_files = [file.replace('_R1_', '_R2_') for file in entered]
            stdout = (subprocess.getoutput("sha1sum {} {}".format(' '.join(entered), ' '.join(r2_files)))).split()
            checksums = {stdout[i]: stdout[i - 1] for i in range(1, len(stdout), 2)}

        for file in entered:
            path, filename = os.path.split(file)
            r2 = file.replace('_R1_', '_R2_')
            _, r2_filename = os.path.split(r2)

            if results[filename] != checksums[file] or results[r2_filename] != checksums[r2]:
                unentered.append(file)
                runs.add(path)
                cb.callback("Re-processing {} and its R2 pair from the database".format(file))

    if len(ignore) > 0 and callback:
        callback("Ignoring {} files".format(len(ignore)))
        for f in ignore:
            callback("\t {}".format(f), level="DEBUG")

    return unentered, runs


def contains_run(run, usr_list):
    """
    Ignores/Includes run if its in the user specified directories

    :param run: str, path to a run
    :param usr_list: list, directories to ignore
    :return: bool, True if the directory is in the file path, False otherwise
    """
    for directory in usr_list:
        if directory in run:
            return True
    return False


def summarize_run_data(usher_barcodes, update_barcodes, freyja_update, path, indir, outdir, callback=None):
    """
    Summarize all sample data for the run into a single JSON file

    :param usher_barcodes: str, path to the usher barcodes file
    :param update_barcodes: bool, option to update the usher barcodes file
    :param freyja_update: date, date when freyja was last
    :param path: str, path to the run
    :param indir: str, path to the input directory
    :param outdir: str, path to the output directory
    :param callback: function, option to print messages to the console
    """

    if update_barcodes or not os.path.isfile(usher_barcodes):
        callback("Downloading usher barcodes...")
        url = 'https://raw.githubusercontent.com/andersen-lab/Freyja/main/freyja/data/usher_barcodes.csv'
        request = requests.get(url, allow_redirects=True)
        usher_barcodes = 'data/usher_barcodes.csv'
        with open(usher_barcodes, 'wb') as file:
            file.write(request.content)    
        
    summarized_output = {'lineagesOfInterest' : {}}
    metadata_list = []
    meta_file = None

    lab, run = path.split(os.path.sep)[-2:]

    # Look for metadata
    metadata_file = glob.glob(os.path.join(path, '*metadata*'))
    if len(metadata_file) != 0:
        try: 
            meta_file = pd.read_csv(metadata_file[0], index_col = 0)
        except:
            sys.stderr.write(f"Unable to read metadata from {path}")        

    
	# read barcode data, for each VOC extract columns that have mutaion
    barcode = pd.read_csv(usher_barcodes, index_col = 0)
    muts = barcode.apply(lambda x: x.index[x == 1].tolist(), axis=1)
    
    results_dir = path.replace(indir, outdir)

    # Assumes all samples have an individual folder with its freyja output files
    samples = [os.path.split(x[0])[1] for x in os.walk(results_dir) if x[0] is not results_dir]

    for sample in samples:
        if sample == "Undetermined":
            callback("Ignoring Undetermined...")
            continue

		# Read in the csv generated from generate_summary to get the lineages 
        csv_summary_file = glob.glob("{}/{}/**/*.freyja.*csv".format(results_dir, sample), recursive=True)
        csv_summary = {}

        # Some samples won't have a summary file because of the cvxpy error
        if len(csv_summary_file) == 0:
            continue

        with open (csv_summary_file[0], 'r') as csvfile:
            reader = DictReader(csvfile)
            csv_summary = {row['name']: {'loi': row['LOI'], 'frequency': float(row['frequency'])} for row in reader}

        variants = [row_key for row_key in csv_summary.keys() if row_key != 'minor']
    
        # Get the var filename
        varfiles = glob.glob("{}/{}/**/var*.tsv".format(results_dir, sample), recursive=True)

        if len(varfiles) == 0:
            send_error_notification(message="Freyja did not process results for {} sample {}".format(results_dir, sample))

        var = pd.read_csv(varfiles[0], sep='\t')
        var['MUT'], var['SAM'] = var.apply(lambda row: row['REF'] + str(row['POS']) + row['ALT'], axis=1), sample
        var = var[['SAM', 'ALT_AA', 'MUT', 'ALT_DP', 'TOTAL_DP']]
        var.columns = ['sample', 'mutation', 'nucleotide', 'count', 'coverage']
        var = var.fillna('')

        for variant in variants:
            tab = var[var['nucleotide'].isin(muts[variant])]
            tab = tab.reset_index(drop = True)
			
            # create more dictionaries
            results = tab.to_dict(orient='index')
            
            if csv_summary[variant]['loi'] not in summarized_output['lineagesOfInterest']:
                summarized_output['lineagesOfInterest'].update({csv_summary[variant]['loi']: {}})
			
            if sample not in summarized_output['lineagesOfInterest'][csv_summary[variant]['loi']]:
                summarized_output['lineagesOfInterest'][csv_summary[variant]['loi']].update({sample: {}})
                
            if variant not in summarized_output['lineagesOfInterest'][csv_summary[variant]['loi']][sample]:
                summarized_output['lineagesOfInterest'][csv_summary[variant]['loi']][sample].update({variant: {'mutInfo': [], 'estimate': {}}})
                
            summarized_output['lineagesOfInterest'][csv_summary[variant]['loi']][sample][variant]['estimate'].update({'est': csv_summary[variant]['frequency'], 'lower.95': '', 'upper.95': '', '__row': sample})
            
            for _, record in results.items():
                summarized_output['lineagesOfInterest'][csv_summary[variant]['loi']][sample][variant]['mutInfo'].append(record)

        # Handle 'minor' lineages
        if 'minor' in csv_summary and csv_summary['minor']['frequency'] != 0:
            if csv_summary['minor']['loi'] not in summarized_output['lineagesOfInterest']:
                summarized_output['lineagesOfInterest'].update({csv_summary['minor']['loi']: {}})
                
            if sample not in summarized_output['lineagesOfInterest'][csv_summary['minor']['loi']]:
                summarized_output['lineagesOfInterest'][csv_summary['minor']['loi']].update({sample: {csv_summary['minor']['loi']: {'estimate': {}}}})
                
            summarized_output['lineagesOfInterest'][csv_summary['minor']['loi']][sample][csv_summary['minor']['loi']]['estimate'].update({'est': csv_summary['minor']['frequency'], 'lower.95': '', 'upper.95': '', '__row': sample})
        
		# Update metadata
        if meta_file is not None:
			# Ignore metadata record if sample cannot be found in the metadata file
            try: 
                site = meta_file.filter(regex="location\sname*")
                date = meta_file.filter(regex="collection\sdate")
                metadata = {'sample': sample, 'lab': lab, 'coldate': date.loc[sample, date.columns.tolist()[0]], 'site': site.loc[sample, site.columns.tolist()[0]]}
                metadata_list.append(metadata)
            except KeyError:
                sys.stderr.write(f"Error: Metadata for sample {sample} from {path} couldn't be found in the metadata file")        

    if meta_file is not None:
        summarized_output.update({'metadata': metadata_list})

    summarized_output.update({'run.dir': [run], 'freyja.last.updated': [freyja_update.strftime('%Y-%m-%d')]})

    freyja_json = os.path.join(results_dir, "{}-{}.json".format(lab, run))
    with open(freyja_json, 'w') as outfile:
        json_output = json.dumps(summarized_output, indent = 2)
        outfile.write(json_output)
    
    # Rename json file
    os.rename(freyja_json, rename_file(freyja_json))


def parse_args():
    """Command-line interface"""
    parser = argparse.ArgumentParser(
        description="Runs minimap2.py on new data"
    )
    parser.add_argument('--db', type=str, default="data/gromstole.db",
                        help="Path to the database")
    parser.add_argument('--indir', type=str, default="/home/wastewater/uploads",
                        help="Path to the uploads directory")
    parser.add_argument('--outdir', type=str, default="/home/wastewater/results/freyja",
                        help="Path to the results directory")
    parser.add_argument('-i', '--ignore-list', nargs="*", default=[],
                        help="Directories to ignore or keywords to ignore in the filename")
    parser.add_argument('--freyjaupdate', dest="freyja_update", type=lambda d: datetime.strptime(d, '%Y-%m-%d').date(), default=date.today(),
	                    help="<option>Date freyja update was last run. Default is today's date")

    parser.add_argument('--minimap2', type=str, default='minimap2',
                        help="<option> path to minimap2 executable")
    parser.add_argument('--cutadapt', type=str, default='cutadapt',
                        help="<option> path to cutadapt executable")
    parser.add_argument('--freyja', type=str, default="freyja",
                        help="<option> path to freyja executable")

    parser.add_argument('--threads', type=int, default=4,
                        help="Number of threads (default 4) for minimap2")
    parser.add_argument('--np', type=int, default=4,
                        help="Number of processes (default 4) to process FASTQ data files")
    parser.add_argument('-t', '--threshold', type=float, default=0.01,
                        help="option, minimum frequency to report lineage (defaults to 0.01)")

    parser.add_argument('--loi', type=str, default="lineages_of_interest.txt",
                        help="input, path to text file listing lineages of interest")
    parser.add_argument('--alias', type=str, default="data/alias_key.json",
                        help="<input> PANGO aliases")
    parser.add_argument('--barcodes', type=str, default="data/usher_barcodes.csv",
                        help="<input> USHER Barcodes")

    parser.add_argument('--sendemail', dest='sendemail', action="store_true",
                        help="<option> send email notification when there is an error")                       
    parser.add_argument('--check', dest='check', action='store_true',
                        help="Check processed files to see if the files have changed since last processing them")
    parser.add_argument('--replace', dest='replace', action='store_false',
                        help="Remove previous result files")
    parser.add_argument('--updatebarcodes', dest='update_barcodes', action='store_true',
                        help="<option>Force update the usher barcodes file")
    parser.add_argument('--rerun', dest='rerun', action='store_true',
                        help="Process result files again (generate JSON, csv and barplots again)")
    parser.add_argument('--runs', nargs="*", default=[],
                        help="Runs to process again")
    parser.set_defaults(check=False, replace=True, rerun=False, sendemail=False, update_barcodes=False)

    return parser.parse_args()


if __name__ == '__main__':
    cb = Callback()
    args = parse_args()
    cb.callback("Starting script")

    args.indir = os.path.normpath(args.indir)
    args.outdir = os.path.normpath(args.outdir)

    files = glob.glob("{}/**/*_R1_*.fastq.gz".format(args.indir), recursive=True)

    if args.rerun:
        runs = get_runs(files, args.ignore_list, args.runs, callback=cb.callback)
    else:
        cursor, connection = open_connection(args.db, callback=cb.callback)
        new_files, runs = get_files(cursor, files, args.ignore_list, args.check, callback=cb.callback)

        # Write the paths to a temporary file
        paths = tempfile.NamedTemporaryFile('w', delete=False)
        for file_path in new_files:
            paths.write('{}\n'.format(file_path))
        paths.close()

        # Temporary files to track which files were processed
        processed_files = tempfile.NamedTemporaryFile('w', delete=False)
        processed_files.close()


        if len(new_files) < args.np:
            args.np = len(new_files)

        cmd = ["mpirun", "-np", str(args.np), 
               "python3", "trim.py", paths.name, processed_files.name, 
               "--freyja", args.freyja,
               "--minimap2", args.minimap2,
               "--cutadapt", args.cutadapt,
                "--outdir", args.outdir, 
                "--indir", args.indir]
        
        if args.sendemail:
            cmd.append("--sendemail")
        
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError:
            sys.stderr.write(f"Error running {' '.join(cmd)}\n")

        # processed = []
        unique_samples = set()
        runs = set()

        # Update database with processed records
        with open(processed_files.name, 'r') as handle:
            for f in handle:
                filepath = f.strip()
                insert_record(cursor, filepath)
                processed_path = os.path.normpath(filepath)
                basepath, sample = os.path.split(processed_path)
                lab, run = basepath.split(os.path.sep)[-2:]
                sname = sample.split('_')[0]
                unique_samples.add(os.path.join(args.outdir, lab, run, sname))
                runs.add(basepath)
                
        # Generate the summary freyja csv files for each sample
        cb.callback("Generating Freyja CSV Files")
        parser = LinParser(args.alias, args.loi)
        for processed_path in unique_samples:
            files = glob.glob(os.path.join(processed_path, "lin.*.tsv"))
            if not files:
                sys.stderr.write(f"ERROR: Directory {processed_path} does not contain any files matching lin.*.tsv\n")
                continue

            # prepare output file
            lab, run, sample = processed_path.split(os.path.sep)[-3:]
            freyja_csv = '{}/{}-{}.freyja.csv'.format(processed_path, lab, sample)
            with open(freyja_csv, 'w') as outfile:
                writer = DictWriter(outfile,
                                        fieldnames=['sample', 'name', 'LOI', 'frequency'])
                writer.writeheader()
                for infile in files:
                    results = parser.parse_lin(infile, threshold=args.threshold)
                    for row in results:
                        writer.writerow(row)

            # Rename file with sha1sum hash
            cb.callback(freyja_csv)
            os.rename(freyja_csv, rename_file(freyja_csv))

        # Generate the json file for each run
        cb.callback("Generating Freyja JSON Files")
        for run in runs:
            summarize_run_data(args.barcodes, args.update_barcodes, args.freyja_update, run, args.indir, args.outdir, callback=cb.callback)


        os.unlink(paths.name)
        os.unlink(processed_files.name)

        connection.commit()
        connection.close()  
    
    if not args.rerun and len(new_files) == 0:
        cb.callback("No new data files")
    else:
        cb.callback("All Done!")     
