import json
import os
import re
import subprocess
import time
from os.path import abspath, join, dirname, splitext, basename
from collections import defaultdict

from Bio import Phylo
from flask import Flask, render_template, send_from_directory, abort, redirect, send_file
from flask import Response, request

from ngs_utils import logger as log
from ngs_utils.file_utils import safe_mkdir, file_transaction, can_reuse

from fingerprinting.model import Project, db, Sample, Run
from fingerprinting.lookups import get_snp_record
from fingerprinting.utils import FASTA_ID_PROJECT_SEPARATOR


def _find_closest_match(sample, run):
    tree = next(Phylo.parse(run.tree_file_path(), 'newick'))
    clade = [c for c in tree.get_terminals() if c.name == sample.long_name()][0]
    other_terminals = [c for c in tree.get_terminals() if c != clade]
    paired_clade = min(other_terminals, key=lambda c2: tree.distance(clade, c2))
    if paired_clade:
        sn, pn = paired_clade.name.split(FASTA_ID_PROJECT_SEPARATOR)
        p = run.projects.get(pn)
        matching_sample = p.samples.get(sn)
        return matching_sample
    else:
        return None


def render_closest_comparison_page(project_names_line, sample_id, selected_idx=None):
    run = Run.find_by_project_names_line(project_names_line)
    if not run:
        log.err('Run ' + str(project_names_line) + ' not found')
        abort(404, {'message': 'Phylogenetic comparison for ' + str(project_names_line) + ' is not found'})
    sample = Sample.query.get(sample_id)
    if not sample:
        log.err('Sample ' + sample_id + ' not found in ' + str(project_names_line))
        abort(404, {'message': 'Sample ' + sample_id + ' not found in ' + str(project_names_line)})
    matching_sample = _find_closest_match(sample, run)
    if not matching_sample:
        log.err('No matching sample for ' + sample.long_name())
        abort(404, {'message': 'No matching sample for ' + sample.long_name()})
    snps_dict = defaultdict(int)
    # snps_dict['total_score'] = 0
    # snps_dict['confidence'] = 'Low'
    snp_tables = []
    snp_records = []
    for i, (snp_a, snp_b) in enumerate(zip(sample.snps_from_run(run),
                                           matching_sample.snps_from_run(run))):
        snp_records.append(get_snp_record(snps_dict, snp_a, snp_b, i + 1))
        if (i + 1) % 40 == 0:
            snp_tables.append(snp_records)
            snp_records = []
    if snp_records:
        snp_tables.append(snp_records)

    bam_fpath_a = '/%s/bamfiles/%s' % (sample.project.name, sample.long_name() + '.bam')
    bam_fpath_b = '/%s/bamfiles/%s' % (matching_sample.project.name, matching_sample.long_name() + '.bam')
    snps_bed = '/%s/snps_bed' % project_names_line
    sample_a = {
        'id': sample.id,
        'name': sample.name,
        'project': sample.project.name,
        'bam': bam_fpath_a,
    }
    sample_b = {
        'id': matching_sample.id,
        'name': matching_sample.name,
        'project': matching_sample.project.name,
        'bam': bam_fpath_b,
    }
    t = render_template(
        'sample.html',
        project_names_line=project_names_line,
        genome=sample.project.genome,
        sampleA=sample_a,
        sampleB=sample_b,
        snps_data=snps_dict,
        snp_tables=snp_tables,
        snps_bed=snps_bed,
        selected_idx=selected_idx or "null",
        total_snps=sum([len(snps) for snps in snp_tables])
    )
    return t


def send_file_for_igv(fpath):
    # handle igv.js Range header which it uses to request a subset of a .bam
    range_header = request.headers.get('Range', None)
    if not range_header:
        return send_file(fpath)

    m = re.search('(\d+)-(\d*)', range_header)
    if not m:
        error_msg = "ERROR: unexpected range header syntax: %s" % range_header
        log.err(error_msg)
        return error_msg

    size = os.path.getsize(fpath)
    offset = int(m.group(1))
    length = int(m.group(2) or size) - offset

    with open(fpath, 'rb') as f:
        f.seek(offset)
        data = f.read(length)

    rv = Response(data, 206, mimetype="application/octet-stream", direct_passthrough=True)
    rv.headers.add('Content-Range', 'bytes {0}-{1}/{2}'.format(offset, offset + length - 1, size))

    log.info("GET range request: %s-%s %s" % (m.group(1), m.group(2), fpath))
    return rv

