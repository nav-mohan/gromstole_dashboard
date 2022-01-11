# Wastewater Surveillance for SARS-CoV-2 Variants of Concern

## Data Format and Processing

We currently receive paired-end FASTQ files generated by the Illumina NGS platform. Each read in these files represents a qRT-PCR read based on the Artic V3 amplicons. 

We process these files by aligning them with the Wuhan-1 genome sequence, determining the mutations relative to this reference, and outputting the list of mutations along with their frequency. The frequency is determined as the number of times that mutation was observed divided by the coverage. The coverage is found by counting the number of reads that have read a base at each position. The coverage at every position (relative to the reference) are also outputted.

To determine VOCs, we need the mutations for that VOC (including how often it was observed in sequences for that variant) as well as the background frequency of the mutations.

- The mutations for a VOC can be found either by processing all sequences in the GISAID database labelled as the VOC or by using the constellation available in the Pangolin naming system.
- The background frequency is found using a pipeline that takes all GISAID sequences, determines their mutations, then counts the number of each mutation lineage divided by the number of times that mutation was observed.

## Methods

Our current modelling strategy consists of a binomial GLM for the count and coverage of the mutations that define a VOC. The VOC-defining mutations are chosen such that they are present in more than 95% of the sequences with that VOC label but fewer than 5% of the remaining data.

The model outputs an estimated proportion of a variant with the given mutations and coverage, along with an estimate of the uncertainty. A hypothesis test for the presence of the VOC can be performed by testing whether 0.01 is in the 95% confidence interval for the estimated proportion. The null 0.01 is used as this is a conservative rule-of-thumb for the sequencing error rates of the Illumina sequencing platform (these platforms also output their own estimated error rate, and incorporating this into the analysis is part of our future work).

## Detecting Relative Frequency VoC's

We have a detailed, fool-proof plan to detect the exact number of Variants of Concern that exist in the relevant population, along with the spatio-temporal spread of these variants, all while accurately determining the actual case counts of Covid-19.
This plan is as follows:

**TODO.**



# The GromStole Pipeline

All commands are intended to be run at the base gromstole directory.

## Processing Samples 

`minimap2` is currently accepts paired-end reads in separate FASTQ files and outputs `[prefix]-mapped.csv` and `[prefix]-coverage.csv` into the specified output directory. It uses `data/NC043312.fa` as a reference genome, but this can be specified by the user. By default, it uses cutadapt on the sequences, but this can be turned off by the user.

Use `python3 scripts/minimap2.py -h` to see all of the options.

```sh
python3 scripts/minimap2.py r1.fastq r2.fastq --outdir results --prefix name-of-sample
```

For our analysis, autoprocess.py runs as a cron job, which monitors a specified directory for uploads and processes them automatically.

## Determining Unique Mutations 

This section can be skipped if you have a pre-specified list of mutations. 

Download the data from NextStrain, then count mutations by lineage, all in one script:

```sh
python3 scripts/retrieve-nsgb.py data/count-mutations-nsgb.json
# Other scripts expect compressed data and it's a smaller file
gzip data/count-mutations-nsgb.json
```


### NextStrain vs. NextStrain

`voi-frequency.py` allows for an arbitrary number of lineage names. It outputs a csv where the first three columns are lineage, a boolean column (0s and 1s) for whether the lineage was one of the input lineage names, and the number of times each lineage was observed in the NextStrain data. 

The remaining columns represent the union of the mutations in all of the specified lineages, and the values represent the fraction of sequences in the specified lineage that contained this mutation.

```sh
python3 scripts/voi-frequency.py B.1.1.529 BA.1 BA.2
```

The output file has a name `data/voi-frequency_B-1-1-529_BA-1_BA-2.csv`, which is generated by replacing the periods in the lineage name with dashes and separating names by underscores.

The following R script will look at the boolean column for which lineages are the variants of interest and use this to determine unique mutations for each row where the entry is 1. For our Omicron example, this means looking at mutations that are in more than 95% of B.1.1.529 AND fewer than 5% of the lineages that have a 0 in the boolean column.

```sh
Rscript scripts/voi-unique.R data/voi-frequency_B-1-1-529_BA-1_BA-2.csv
```

This will output a file for each of the variants of interest, with a name similar to `lineages/B-1-1-529_YYYY-MM-DD.csv` with colums for the type, position, alt and label of the mutations.

## Run Analysis 

Given a directory containing one or more subdirectories that each contain paired-end Illumina FASTQ fiiles, the following R script runs the binomial regression and outputs a json file that contains all relevant information:

- the counts of each mutation of the `lineages/` file
- the coverage at every position on the reference genome, 
- the metadata that was used as input (optional), 
- the estimate of the proportion (including 95% confidence interval), 
- the lineage name, and 
- the name of the input directory.

```sh
Rscript scripts/estimate-freqs.R results/name-of-sample lineages/B-1-1-529_YYYY-MM-DD.csv results/outfile_B-1-1-529.json path/to/metadata.csv
```

To get a nice summary of the results, the following scripts produces a very pretty and informative bar plot with the specified filename.

```sh
Rscript scripts/make-barplots.R results/outfile_B-1-1-529.json results/barplot_B-1-1-529.pdf
```
