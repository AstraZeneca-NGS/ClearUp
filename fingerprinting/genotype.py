import shutil
from collections import OrderedDict

import os
from os.path import join, dirname, splitext, basename
import sys
from cyvcf2 import VCF, Writer
from subprocess import check_output
from variant_filtering.filtering import combine_vcfs

from ngs_utils.bed_utils import bgzip_and_tabix
from ngs_utils.call_process import run
from ngs_utils.utils import is_local, is_us
from ngs_utils.parallel import ParallelCfg, parallel_view
from pybedtools import BedTool

from ngs_utils.file_utils import file_transaction, safe_mkdir, chdir, which, adjust_path, can_reuse, add_suffix, \
    verify_file, intermediate_fname, splitext_plus
from ngs_utils.logger import info, err, critical, debug, warn
from ngs_utils.sambamba import index_bam

import az

from fingerprinting.utils import is_sex_chrom


DEPTH_CUTOFF = 5


def genotype(samples, snp_bed, parall_view, output_dir, genome_build):
    genome_cfg = az.get_refdata(genome_build)
    info('** Running VarDict ** ')
    vcfs = parall_view.run(_vardict_pileup_sample,
        [[s, safe_mkdir(join(output_dir, 'vcf')), genome_cfg, parall_view.cores_per_job, snp_bed]
         for s in samples])
    vcf_by_sample = OrderedDict(zip([s.name for s in samples], vcfs))
    info('** Finished running VarDict **')
    return vcf_by_sample
    
    
def post_genotype(samples, vcf_by_sample, snp_bed, parall_view, output_dir, work_dir, depth_cutoff=DEPTH_CUTOFF):
    info('** Remove sex chromosomes **')
    autosomal_vcf_by_sample = OrderedDict()
    for sn, vf in vcf_by_sample.items():
        autosomal_vcf_by_sample[sn] = add_suffix(vf, 'autosomal')
        run('grep -v chrX ' + vf + ' | grep -v chrY', output_fpath=autosomal_vcf_by_sample[sn])
    
    info('** Annotate variants with gene names and rsIDs **')
    vcf_files = parall_view.run(_annotate_vcf, [[autosomal_vcf_by_sample[s.name], snp_bed] for s in samples])
    ann_vcf_by_sample = {s.name: vf for s, vf in zip(samples, vcf_files)}

    info('** Writing fasta **')
    fasta_work_dir = safe_mkdir(join(work_dir, 'fasta'))
    fasta_by_sample = OrderedDict((s.name, join(fasta_work_dir, s.name + '.fasta')) for s in samples)
    parall_view.run(vcf_to_fasta, [[s, ann_vcf_by_sample[s.name], fasta_by_sample[s.name], depth_cutoff]
                                   for s in samples])
    info('** Merging fasta **')
    all_fasta = join(output_dir, 'fingerprints.fasta')
    if not can_reuse(all_fasta, fasta_by_sample.values()):
        with open(all_fasta, 'w') as out_f:
            for s in samples:
                with open(fasta_by_sample[s.name]) as f:
                    out_f.write(f.read())
    info('All fasta saved to ' + all_fasta)

    info('Saving VCFs into the output directory')
    for sn, vf in ann_vcf_by_sample.items():
        shutil.copy(vf, join(output_dir, basename(vcf_by_sample[sn]) + '.gz'))
        shutil.copy(vf, join(output_dir, basename(vcf_by_sample[sn]) + '.gz.tbi'))

    # info('Combining VCFs')
    # combined_vcf = combine_vcfs(vcf_by_sample.values(), join(output_dir, 'fingerprints.vcf'))
    # assert combined_vcf

    # ped_file = splitext_plus(combined_vcf)[0]
    # cmdl = 'vcftools --plink --gzvcf {combined_vcf}'.format(**locals())
    # cmdl += ' --out ' + splitext(ped_file)[0]
    # run(cmd=cmdl)
    # info('Converting VCFs to PED')
    # ped_file = vcf_to_ped(vcf_by_sample, join(output_dir, 'fingerprints.ped'), sex_by_sample, depth_cutoff)
    
    return all_fasta, ann_vcf_by_sample


# def vcf_to_ped(vcf_by_sample, ped_file, sex_by_sample, depth_cutoff):
#     if can_reuse(ped_file, vcf_by_sample.values()):
#         return ped_file
#
#     from cyvcf2 import VCF
#     with file_transaction(None, ped_file) as tx:
#         with open(tx, 'w') as out_f:
#             for sname, vcf_file in vcf_by_sample.items():
#                 recs = [v for v in VCF(vcf_file)]
#                 sex = {'M': '1', 'F': '2'}.get(sex_by_sample[sname], '0')
#                 out_fields = [sname, sname, '0', '0', sex, '0']
#                 for rec in recs:
#                     gt = vcfrec_to_seq(rec, depth_cutoff).replace('N', '0')
#                     out_fields.extend([gt[0], gt[1]])
#                 out_f.write('\t'.join(out_fields) + '\n')
#
#     info('PED saved to ' + ped_file)
#     return ped_file


def genotype_bcbio_proj(proj, snp_bed, parallel_cfg, depth_cutoff=DEPTH_CUTOFF,
                        output_dir=None, work_dir=None):
    output_dir = output_dir or safe_mkdir(join(proj.date_dir, 'fingerprints'))
    work_dir = work_dir or safe_mkdir(proj.work_dir)
    with parallel_view(len(proj.samples), parallel_cfg, work_dir) as parall_view:
        vcf_by_sample = genotype(proj.samples, snp_bed, parall_view, work_dir, proj.genome_build)
        fasta_file, vcf_by_sample = post_genotype(proj.samples, vcf_by_sample,
            snp_bed, parall_view, output_dir, work_dir, depth_cutoff=depth_cutoff)
    return vcf_by_sample
    

def _split_bed(bed_file, work_dir):
    """ Splits into autosomal and sex chromosomes
    """
    autosomal_bed = intermediate_fname(work_dir, bed_file, 'autosomal')
    sex_bed = intermediate_fname(work_dir, bed_file, 'sex')
    if not can_reuse(autosomal_bed, bed_file) or not can_reuse(sex_bed, bed_file):
        with open(bed_file) as f, open(autosomal_bed, 'w') as a_f, open(sex_bed, 'w') as s_f:
            for l in f:
                chrom = l.split()[0]
                if is_sex_chrom(chrom):
                    s_f.write(l)
                else:
                    a_f.write(l)
    return autosomal_bed, sex_bed


def _vardict_pileup_sample(sample, output_dir, genome_cfg, threads, snp_file):
    vardict_snp_vars = join(output_dir, sample.name + '_vars.txt')
    vardict_snp_vars_vcf = join(output_dir, sample.name + '.vcf')
    
    if can_reuse(vardict_snp_vars, [sample.bam, snp_file]) and can_reuse(vardict_snp_vars_vcf, vardict_snp_vars):
        return vardict_snp_vars_vcf
    
    if is_local():
        vardict_dir = '/Users/vlad/vagrant/VarDict/'
    elif is_us():
        vardict_dir = '/group/cancer_informatics/tools_resources/NGS/bin/'
    else:
        vardict_pl = which('vardict.pl')
        if not vardict_pl:
            critical('Error: vardict.pl is not in PATH')
        vardict_dir = dirname(vardict_pl)

    # Run VarDict
    index_bam(sample.bam)
    vardict = join(vardict_dir, 'vardict.pl')
    ref_file = adjust_path(genome_cfg['seq'])
    cmdl = '{vardict} -G {ref_file} -N {sample.name} -b {sample.bam} -p -D {snp_file}'.format(**locals())
    run(cmdl, output_fpath=vardict_snp_vars)
    
    # Convert to VCF
    cmdl = ('cut -f-34 ' + vardict_snp_vars +
            ' | awk -F"\\t" -v OFS="\\t" \'{for (i=1;i<=NF;i++) { if ($i=="") $i="0" } print $0 }\''
            ' | ' + join(vardict_dir, 'teststrandbias.R') +
            ' | ' + join(vardict_dir, 'var2vcf_valid.pl') +
            # ' | grep "^#\|TYPE=SNV\|TYPE=REF" ' +
            '')
    run(cmdl, output_fpath=vardict_snp_vars_vcf)
    _fix_vcf(vardict_snp_vars_vcf, ref_file)
    
    return vardict_snp_vars_vcf


def _fix_vcf(vardict_snp_vars_vcf, ref_file):
    """ Fixes VCF generated by VarDict in puleup debug mode:
        - Fix non-call records with empty REF and LAT, and "NA" values assigned to INFO's SN and HICOV
    :param vardict_snp_vars_vcf: VarDict's VCF in pileup debug mode
    """
    vardict_snp_vars_fixed_vcf = add_suffix(vardict_snp_vars_vcf, 'fixed')
    info('Fixing VCF, writing to ' + vardict_snp_vars_fixed_vcf)
    with open(vardict_snp_vars_vcf) as inp, open(vardict_snp_vars_fixed_vcf, 'w') as out_f:
        for l in inp:
            if l.startswith('#'):
                out_f.write(l)
            else:
                fs = l.split('\t')
                chrom, start, ref, alt = fs[0], fs[1], fs[3], fs[4]
                if ref in ['.', '']:
                    samtools = which('samtools')
                    if not samtools: sys.exit('Error: samtools not in PATH')
                    cmdl = '{samtools} faidx {ref_file} {chrom}:{start}-{start}'.format(**locals())
                    faidx_out = check_output(cmdl, shell=True)
                    fasta_ref = faidx_out.split('\n')[1].strip().upper()
                    assert faidx_out
                    fs[3] = fasta_ref  # '.'  # REF
                    fs[4] = fasta_ref  # '.'  # ALT
                    l = '\t'.join(fs)
                    l = l.replace('=NA;', '=.;')
                    l = l.replace('=;', '=.;')
                if len(ref) == len(alt) == 1:  # SNP (not indel or complex variant)
                    out_f.write(l)
            
    assert verify_file(vardict_snp_vars_fixed_vcf)
    os.rename(vardict_snp_vars_fixed_vcf, vardict_snp_vars_vcf)
    return vardict_snp_vars_vcf


def _annotate_vcf(vcf_file, snp_bed):
    gene_by_snp = dict()
    rsid_by_snp = dict()
    for interval in BedTool(snp_bed):
        rsid, gene = interval.name.split('|')
        pos = int(interval.start) + 1
        gene_by_snp[(interval.chrom, pos)] = gene
        rsid_by_snp[(interval.chrom, pos)] = rsid
    
    vcf_file = bgzip_and_tabix(vcf_file)
    ann_vcf_file = add_suffix(vcf_file, 'ann')
    vcf = VCF(vcf_file)
    vcf.add_info_to_header({'ID': 'GENE', 'Description': 'Overlapping gene', 'Type': 'String', 'Number': '1'})
    vcf.add_info_to_header({'ID': 'rsid', 'Description': 'dbSNP rsID', 'Type': 'String', 'Number': '1'})
    w = Writer(ann_vcf_file, vcf)
    for rec in vcf:
        rec.INFO['GENE'] = gene_by_snp[(rec.CHROM, rec.POS)]
        rec.INFO['rsid'] = rsid_by_snp[(rec.CHROM, rec.POS)]
        w.write_record(rec)
    w.close()
    assert verify_file(ann_vcf_file), ann_vcf_file
    os.rename(ann_vcf_file, vcf_file)
    return bgzip_and_tabix(vcf_file)


# def __p(rec):
#     s = rec.samples[0]
#     gt_type = {
#         0: 'hom_ref',
#         1: 'het',
#         2: 'hom_alt',
#         None: 'uncalled'
#     }.get(s.gt_type)
#     gt_bases = s.gt_bases
#     return str(rec) + ' FILTER=' + str(rec.FILTER) + ' gt_bases=' + str(gt_bases) + ' gt_type=' + gt_type + ' GT=' + str(s['GT']) + ' Depth=' + str(s['VD']) + '/' + str(s['DP'])


def vcfrec_to_seq(rec, depth_cutoff):
    called = rec.num_called > 0
    depth_failed = rec.INFO['DP'] < depth_cutoff
    filter_failed = rec.FILTER and any(v in ['MSI12', 'InGap'] for v in rec.FILTER)
    if depth_failed or filter_failed:
        called = False

    if is_sex_chrom(rec.CHROM):  # We cannot confidentelly determine sex, and thus determine X heterozygocity,
        gt_bases = ''            # so we can't promise constant fingerprint length across all samples
    elif called:
        gt_bases = ''.join(sorted(rec.gt_bases[0].split('/')))
    else:
        gt_bases = 'NN'
    
    return gt_bases


# def calc_avg_depth(vcf_file):
#     with open(vcf_file) as f:
#         vcf_reader = vcf.Reader(f)
#         recs = [r for r in vcf_reader]
#     depths = [r.INFO['DP'] for r in recs]
#     return float(sum(depths)) / len(depths)


# def check_if_male(recs):
#     y_total_depth = 0
#     for rec in recs:
#         depth = rec.INFO['DP']
#         if 'Y' in rec.CHROM:
#             y_total_depth += depth
#     return y_total_depth >= 5


def vcf_to_fasta(sample, vcf_file, fasta_file, depth_cutoff):
    if can_reuse(fasta_file, vcf_file):
        return fasta_file

    from cyvcf2 import VCF
    info('Parsing VCF ' + vcf_file)
    recs = [v for v in VCF(vcf_file)]
    with open(fasta_file, 'w') as fhw:
        fhw.write('>' + sample.name + '\n')
        fhw.write(''.join(vcfrec_to_seq(rec, depth_cutoff) for rec in recs) + '\n')

    info('Fasta saved to ' + fasta_file)
