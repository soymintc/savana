"""
SAVANA strucural variant caller for long-read data - main program
Created: 21/09/2021
Python 3.9.6
Hillary Elrick
"""
#!/usr/bin/env python3

import sys
import os
import argparse

from time import time
from math import ceil
from pathlib import Path
from multiprocessing import Pool, cpu_count

import pysam

import savana.helper as helper
import savana.validation as validation
from savana.breakpoints import *
from savana.clusters import *

logo = """
███████  █████  ██    ██  █████  ███    ██  █████
██      ██   ██ ██    ██ ██   ██ ████   ██ ██   ██
███████ ███████ ██    ██ ███████ ██ ██  ██ ███████
     ██ ██   ██  ██  ██  ██   ██ ██  ██ ██ ██   ██
███████ ██   ██   ████   ██   ██ ██   ████ ██   ██
"""

def pool_get_potential_breakpoints(bam_files, args):
	""" split the genome into 500kBp chunks and identify PotentialBreakpoints """
	chunk_size = 500000 # 5.0e5 or (half a million)
	pool_potential = Pool(processes=args.threads)
	pool_potential_args = []
	contigs_to_consider = helper.get_contigs(args.contigs, args.ref_index)
	for label, bam_file in bam_files.items():
		for contig in bam_file.get_index_statistics():
			if contig.contig not in contigs_to_consider:
				if args.debug:
					print(f'Skipping reads aligned to {contig.contig} - not in contigs file')
				continue
			if contig.mapped == 0:
				continue
			chrom_length = int(bam_file.get_reference_length(contig.contig))
			if chrom_length > chunk_size:
				# split the chrom into parts
				num_intervals = ceil(chrom_length/chunk_size) + 1
				start_pos = 0
				for i in range(1, num_intervals):
					end_pos = start_pos + chunk_size
					end_pos = chrom_length if end_pos > chrom_length else end_pos # don't extend past end
					pool_potential_args.append((bam_file.filename, args, label, contigs_to_consider, contig.contig, start_pos, end_pos))
					start_pos = end_pos + 1
			else:
				pool_potential_args.append((bam_file.filename, args, label, contigs_to_consider, contig.contig))
	potential_breakpoints_results = pool_potential.starmap(get_potential_breakpoints, pool_potential_args)
	pool_potential.close()
	pool_potential.join()
	return potential_breakpoints_results

def pool_cluster_breakpoints(args, chrom_potential_breakpoints):
	""" perform initial clustering of Potential Breakpoints """
	pool_clustering = Pool(processes=args.threads)
	pool_clustering_args = []
	for breakpoints in chrom_potential_breakpoints.values():
		pool_clustering_args.append((breakpoints, args))
	clustering_results = pool_clustering.starmap(cluster_breakpoints, pool_clustering_args)
	pool_clustering.close()
	pool_clustering.join()
	clusters = {} # collect results
	for result in clustering_results:
		for bp_type in ["+-", "++", "-+", "--", "<INS>"]:
			clusters.setdefault(bp_type, []).extend(result[bp_type])
	return clusters

def pool_output_clusters(args, clusters, outdir):
	""" output trimmed fastqs of the reads in each cluster """
	pool_output = Pool(processes=args.threads)
	pool_output_args = []
	# split list into equal chunks from https://stackoverflow.com/a/2135920
	quotient, remainder = divmod(len(clusters), args.threads)
	clusters_split = (clusters[i*quotient+min(i, remainder):(i+1)*quotient+min(i+1, remainder)] for i in range(args.threads))
	for split in clusters_split:
		pool_output_args.append((split, outdir))
	pool_output.starmap(output_clusters, pool_output_args)
	pool_output.close()
	pool_output.join()

def spawn_processes(args, bam_files, checkpoints, time_str, outdir):
	""" run main algorithm steps in parallel processes """
	print(f'Using multiprocessing with {args.threads} threads\n')
	# 1) GET POTENTIAL BREAKPOINTS
	potential_breakpoints_results = pool_get_potential_breakpoints(bam_files, args)
	if args.debug:
		time_function("Identified potential breakpoints", checkpoints, time_str)
	# collect results per chrom
	chrom_potential_breakpoints = {}
	for result in potential_breakpoints_results:
		for chrom, potential_breakpoints in result.items():
			chrom_potential_breakpoints.setdefault(chrom,[]).extend(potential_breakpoints)

	# 2) CLUSTER POTENTIAL BREAKPOINTS
	clusters = pool_cluster_breakpoints(args, chrom_potential_breakpoints)
	if args.debug:
		time_function("Clustered potential breakpoints", checkpoints, time_str)

	if args.debug:
		# 2.1) OUTPUT CLUSTERS
		for bp_type in ["+-", "++", "-+", "--", "<INS>"]:
			pool_output_clusters(args, clusters[bp_type], outdir)
		time_function("Output originating clusters", checkpoints, time_str)

	# 3) CALL BREAKPOINTS
	breakpoints = call_breakpoints(clusters, args.buffer)
	if args.debug:
		time_function("Called consensus breakpoints", checkpoints, time_str)

	# 3.1) OUTPUT BREAKPOINTS
	bedpe_string = ''
	vcf_string = helper.generate_vcf_header(args.ref, args.ref_index, args.tumour, breakpoints[0])
	read_support_string = ''
	ref_fasta = pysam.FastaFile(args.ref)
	for count, bp in enumerate(breakpoints):
		bedpe_string += bp.as_bedpe(count)
		vcf_string += bp.as_vcf(ref_fasta)
		read_support_string += bp.as_read_support(count)
	with open(os.path.join(outdir, f'{args.sample}.sv_breakpoints.vcf'), 'w') as output:
		output.write(vcf_string)
	with open(os.path.join(outdir, f'{args.sample}.sv_breakpoints.bedpe'), 'w') as output:
		output.write(bedpe_string)
	with open(os.path.join(outdir, f'{args.sample}.sv_breakpoints_read_support.tsv'), 'w') as output:
		output.write(read_support_string)
	if args.debug:
		time_function("Output consensus breakpoints", checkpoints, time_str)

	return clusters, breakpoints, checkpoints, time_str

def time_function(desc, checkpoints, time_str, final=False):
	""" prints the number of seconds elapsed compared to previous checkpoint """
	checkpoints.append(time())
	if not final:
		formatted_time = f'{desc:<40}{round(checkpoints[-1] - checkpoints[-2], 2)} seconds'
	else:
		formatted_time = f'{desc:<40}{round(checkpoints[-1] - checkpoints[0], 2)} seconds'
	time_str.append(formatted_time)
	print(formatted_time)
	return

def savana_run(args):
	""" main function for SAVANA """
	if not args.sample:
		# set sample name to default if req.
		args.sample = os.path.splitext(os.path.basename(args.tumour))[0]
	print(f'Running as sample {args.sample}')

	# create output dir if it doesn't exist
	outdir = os.path.join(os.getcwd(), args.outdir)
	if not os.path.exists(outdir):
		print(f'Creating directory {outdir} to store results')
		os.mkdir(outdir)
	elif os.listdir(outdir):
		sys.exit(f'Output directory "{outdir}" already exists and contains files. Please remove the files or supply a different directory name.')

	# set number of threads to cpu count if none set
	if not args.threads:
		args.threads = cpu_count()

	# read bam files (must have bai)
	bam_files = {
		'tumour': pysam.AlignmentFile(args.tumour, "rb"),
		'normal': pysam.AlignmentFile(args.normal, "rb")
	}

	# confirm ref and ref fasta index exist
	if not os.path.exists(args.ref):
		sys.exit(f'Provided reference: "{args.ref}" does not exist. Please provide full path')
	elif args.ref_index and not os.path.exists(args.ref_index):
		sys.exit(f'Provided reference fasta index: "{args.ref_index}" does not exist. Please provide full path')
	elif not os.path.exists(f'{args.ref}.fai'):
		sys.exit(f'Default reference fasta index: "{args.ref}.fai" does not exist. Please provide full path')
	else:
		args.ref_index = f'{args.ref}.fai' if not args.ref_index else args.ref_index
		print(f'Using {args.ref_index} as reference fasta index')

	# initialize timing
	checkpoints = [time()]
	time_str = []

	# run SAVANA processes
	consensus_clusters, breakpoints, checkpoints, time_str = spawn_processes(args, bam_files, checkpoints, time_str, outdir)

	if args.debug:
		write_cluster_bed(consensus_clusters, outdir)
		calculate_cluster_stats(consensus_clusters, outdir)

	if args.debug:
		time_function("Applied somatic filters", checkpoints, time_str)

	# validate vcf
	output_vcf = os.path.join(outdir, f'{args.sample}.sv_breakpoints.vcf')
	if args.validation:
		try:
			validation.validate_vcf(outdir, output_vcf, args.validation)
		except Exception as e:
			print(f'\nWARNING: Validation of breakpoints against {args.validation} failed due to "{str(e)}"')
			print(f'You can retry by running "python savana/validation.py --outdir testing --input {output_vcf} --validation {args.validation}"')

	time_function("Total time", checkpoints, time_str, final=True)
	f = open(os.path.join(outdir, 'time.log'), "w+")
	f.write("\n".join(time_str))
	f.write("\n")
	f.close()

def savana_evaluate(args):
	""" main function for savana evaluate """
	# create output dir if it doesn't exist
	outdir = os.path.join(os.getcwd(), args.outdir)
	if not os.path.exists(outdir):
		print(f'Creating directory {outdir} to store results')
		os.mkdir(outdir)
	elif os.listdir(outdir):
		sys.exit(f'Output directory "{outdir}" already exists and contains files. Please remove the files or supply a different directory name.')
	else:
		print(f'Using {outdir} to store output')

	if not os.path.exists(args.input):
		sys.exist(f'Provided input vcf: "{args.input}" does not exist. Please provide full path')
	if not os.path.exists(args.validation):
		sys.exist(f'Provided validation vcf: "{args.validation}" does not exist. Please provide full path')

	validation.validate_vcf(outdir, args.input, args.validation)
	print("Done.")

def main():
	""" main function for SAVANA - collects command line arguments and executes algorithm """

	# parse arguments - separated into subcommands
	global_parser = argparse.ArgumentParser(prog="savana", description="SAVANA - somatic SV caller")
	global_parser.add_argument('--version', action='version', version=f'SAVANA {helper.__version__}')
	subparsers = global_parser.add_subparsers(title="subcommands", help='SAVANA sub-commands')

	# savana run
	run_parser = subparsers.add_parser("run", help="run SAVANA on tumour and normal long-read BAMs to detect SVs")
	run_parser.add_argument('--tumour', nargs='?', type=str, required=True, help='Tumour BAM file (must have index)')
	run_parser.add_argument('--normal', nargs='?', type=str, required=True, help='Normal BAM file (must have index)')
	run_parser.add_argument('--ref', nargs='?', type=str, required=True, help='Full path to reference genome')
	run_parser.add_argument('--ref_index', nargs='?', type=str, required=False, help='Full path to reference genome fasta index (ref path + ".fai" by default)')
	run_parser.add_argument('--contigs', nargs='?', type=str, help="Contigs/chromosomes to consider (optional, default=All)")
	run_parser.add_argument('--length', nargs='?', type=int, default=30, help='Minimum length SV to consider (default=30)')
	run_parser.add_argument('--mapq', nargs='?', type=int, default=5, help='MAPQ filter on reads which are considered (default=5)')
	run_parser.add_argument('--buffer', nargs='?', type=int, default=10, help='Buffer to add when clustering adjacent potential breakpoints (default=10)')
	run_parser.add_argument('--depth', nargs='?', type=int, default=3, help='Threshold number of supporting reads required to keep a cluster (default=3)')
	run_parser.add_argument('--threads', nargs='?', type=int, const=0, help='Number of threads to use (default=max)')
	run_parser.add_argument('--outdir', nargs='?', required=True, help='Output directory (can exist but must be empty)')
	run_parser.add_argument('--sample', nargs='?', type=str, help="Name to prepend to output files (default=tumour BAM filename without extension)")
	run_parser.add_argument('--debug', action='store_true', help='Output extra debugging info and files')
	run_parser.add_argument('--validation', nargs='?', type=str, required=False, help='VCF file to use as validation (optional)')
	run_parser.set_defaults(func=savana_run)

	# savana evaluate
	evaluate_parser = subparsers.add_parser("evaluate", help="evaluate and label SAVANA VCF given a VCF to evaluate against")
	evaluate_parser.add_argument('--outdir', nargs='?', required=True, help='Output directory (can exist but must be empty)')
	evaluate_parser.add_argument('--input', nargs='?', type=str, required=False, help='VCF file to evaluate')
	evaluate_parser.add_argument('--validation', nargs='?', type=str, required=False, help='VCF file to evaluate against')
	evaluate_parser.set_defaults(func=savana_evaluate)

	args = global_parser.parse_args()
	print(logo)
	print(f'Version {helper.__version__} - beta')
	src_location = __file__
	print(f'Source: {src_location}\n')
	args.func(args)

if __name__ == "__main__":
	main()
