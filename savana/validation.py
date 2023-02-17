"""
SAVANA strucural variant caller for long-read data - validation sub-command
Created: 21/09/2021
Python 3.9.6
Hillary Elrick
"""
#!/usr/bin/env python3

import re
import os
import csv

import savana.helper as helper
from savana.breakpoints import *
from savana.clusters import *

def validate_vcf(outdir, compare_vcf, validation_vcf):
	""" compare output vcf with 'truthset' validation vcf """
	validation_str = []
	validation_str.append(f'\nEvaluating compared to provided validation file: \'{validation_vcf}\'')
	truthset = []
	chrom_starts = None
	with open(validation_vcf) as f:
		reader = csv.reader(f, delimiter='\t')
		for line in list(reader):
			if line[0].startswith("#"):
				continue
			sv_type = re.search(r"SVTYPE=([A-Z]*)", line[7])
			try:
				sv_length_match = re.search(r"SVLEN=([0-9]*)", line[7])
				sv_length = sv_length_match.group(1)
			except Exception as e:
				sv_length = 0 # sometimes not available
			if not chrom_starts:
				if line[0].startswith("chr"):
					chrom_starts = True
				else:
					chrom_starts = False
			truthset.append({
				'start_chr': line[0],
				'start_loc': int(line[1]),
				'label': line[2],
				'alt': line[3],
				'info': line[7],
				'type': sv_type.group(1),
				'length': sv_length,
				'incorrectly_categorized': False,
				'seen': False
			})
	compareset = []
	with open(compare_vcf) as f:
		reader = csv.reader(f, delimiter='\t')
		for line in list(reader):
			if line[0].startswith("#"):
				continue
			#sv_type = re.search(r"SVTYPE=([A-Z]*)", line[7])
			sv_length = re.search(r"SVLEN=([0-9]*)", line[7])
			tumour_support = re.search(r"TUMOUR_SUPPORT=([0-9]*)", line[7])
			normal_support = re.search(r"NORMAL_SUPPORT=([0-9]*)", line[7])
			clusters = re.search(r"CLUSTER=([0-9,a-z]*)",line[7])
			bp_notation = re.search(r"BP_NOTATION=([\+,\-,\<INS\>]*)",line[7])
			# convert to same chrom format as truthset
			if chrom_starts and not line[0].startswith('chr'):
				converted_chrom = 'chr'+line[0]
			elif not chrom_starts and line[0].startswith('chr'):
				converted_chrom = line[0][3:]
			else:
				converted_chrom = line[0] # no need to convert

			# put into a dict
			compareset_entry = {
				'start_chr': converted_chrom,
				'start_loc': int(line[1]),
				'label': line[2],
				'alt': line[3],
				'info': line[7],
				'validated': False
			}
			# savana-specific variables
			if clusters:
				compareset_entry['clusters'] = clusters.group(1)
			if bp_notation:
				compareset_entry['bp_type'] = bp_notation.group(1)
			if sv_length:
				compareset_entry['length'] = sv_length.group(1)
				compareset_entry['tumour_support'] = int(tumour_support.group(1)) if tumour_support else 0
				compareset_entry['normal_support'] = int(normal_support.group(1)) if normal_support else 0
			compareset.append(compareset_entry)
	# sort by support so that those with less normal support and more tumour support are seen first
	somatic_compareset = list(sorted(compareset, key=lambda x: (x['normal_support'], (-1)*x['tumour_support'])))
	# get number of true positives, false positives, and false negatives
	tp, fp, fn = [], [], []
	already_validated = {}
	for bp in somatic_compareset:
		# remove chr prefix to compare
		for ts in truthset:
			# compare every somatic variant to each truthset variant
			if ts['start_chr'] == bp['start_chr']:
				if not ts['seen'] and not bp['validated'] and abs(ts['start_loc']-bp['start_loc']) <= 100:
					ts['seen'] = True
					ts['start_compare'] = abs(ts['start_loc']-bp['start_loc'])
					ts['clusters'] = bp['clusters'] if 'clusters' in bp else None
					bp['validated'] = True
					tp.append(bp)
					already_validated.pop(bp['label'], None)
				elif not bp['validated'] and abs(ts['start_loc']-bp['start_loc']) <= 100:
					already_validated[bp['label']] = [ts['label'], ts['clusters']]

	validation_str.append('\nDUPLICATE BREAKPOINTS')
	for bp_label, ts_info in already_validated.items():
		validation_str.append(f'{ts_info[0]} validated by {ts_info[1]} - {bp_label} marked as FP')

	fp = [bp for bp in somatic_compareset if not bp['validated']]
	fn = [ts for ts in truthset if not ts['seen']]

	validation_str.append('\nMISSED BREAKPOINTS')
	for bp in fn:
		validation_str.append(f'{bp["label"]} not validated')

	# calculate support by sv type
	sv_type_counts = {}
	for ts in truthset:
		if ts['type'] not in sv_type_counts:
			counts = {
				'seen': 0 if not ts['seen'] else 1,
				'missed': 0 if ts['seen'] else 1
			}
			sv_type_counts[ts['type']] = counts
		else:
			counts = {
				'seen': sv_type_counts[ts['type']]['seen'] if not ts['seen'] else sv_type_counts[ts['type']]['seen']+1,
				'missed': sv_type_counts[ts['type']]['missed'] if ts['seen'] else sv_type_counts[ts['type']]['missed']+1,
			}
			sv_type_counts[ts['type']] = counts

	validation_str.append('\nEVALUATION OF BREAKPOINTS')
	validation_str.append(f'True Positives: {len(tp)}')
	validation_str.append(f'False Positives: {len(fp)}')
	validation_str.append(f'False Negatives: {len(fn)}')
	try:
		precision = len(tp)/(len(tp)+len(fp))
		recall = len(tp)/(len(tp)+len(fn))
		f_measure = (2*precision*recall)/(precision+recall)
		validation_str.append(f'\nSTATISTICS')
		validation_str.append(f'Precision: {round(precision, 3)}')
		validation_str.append(f'Recall: {round(recall, 3)}')
		validation_str.append(f'F-measure: {round(f_measure, 3)}')
		#validation_str.append(f'{len(miscategorized)} True Variants Miscategorized as Germline')
	except ZeroDivisionError as e:
		print("WARNING: Unable to calculate validation statistics due to divide by zero exception")
	except Exception as e:
		print(f'WARNING: Unable to calculate validation statistics due to "{str(e)}"')

	validation_str.append('\nVALIDATION BY SV TYPE')
	for sv_type, values in sv_type_counts.items():
		pcnt = round(values['seen']/(values['missed']+values['seen'])*100, 2)
		validation_str.append(f'{sv_type}: identified {values["seen"]} of {values["seen"]+values["missed"]} ({pcnt}%)')

	# output validation statistics
	f = open(os.path.join(outdir, f'validation.stats'), "w+")
	for string in validation_str:
		f.write(string+"\n")
	f.close()

	return

if __name__ == "__main__":
	print("Validation functions for SAVANA")
