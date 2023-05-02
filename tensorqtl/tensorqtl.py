#!/usr/bin/env python3
from __future__ import print_function
import pandas as pd
import numpy as np
from datetime import datetime
import sys
import os
import pickle
import argparse

sys.path.insert(1, os.path.dirname(__file__))
from core import *
from post import *
import genotypeio, cis, trans, susie


def main():
    parser = argparse.ArgumentParser(description='tensorQTL: GPU-based QTL mapper')
    parser.add_argument('genotype_path', help='Genotypes in PLINK format')
    parser.add_argument('phenotype_bed', help='Phenotypes in BED format')
    parser.add_argument('prefix', help='Prefix for output file names')
    parser.add_argument('--mode', default='cis', choices=['cis', 'cis_nominal', 'cis_independent', 'cis_susie', 'trans'], help='Mapping mode. Default: cis')
    parser.add_argument('--covariates', default=None, help='Covariates file, tab-delimited, covariates x samples')
    parser.add_argument('--paired_covariate', default=None, help='Single phenotype-specific covariate. Tab-delimited file, phenotypes x samples')
    parser.add_argument('--permutations', type=int, default=10000, help='Number of permutations. Default: 10000')
    parser.add_argument('--L', type=int, default=10, help='SuSiE L. Default: 10')
    parser.add_argument('--interaction', default=None, type=str, help='Interaction term(s)')
    parser.add_argument('--cis_output', default=None, type=str, help="Output from 'cis' mode with q-values. Required for independent cis-QTL mapping.")
    parser.add_argument('--phenotype_groups', default=None, type=str, help='Phenotype groups. Header-less TSV with two columns: phenotype_id, group_id')
    parser.add_argument('--window', default=1000000, type=np.int32, help='Cis-window size, in bases. Default: 1000000.')
    parser.add_argument('--pval_threshold', default=None, type=np.float64, help='Output only significant phenotype-variant pairs with a p-value below threshold. Default: 1e-5 for trans-QTL')
    parser.add_argument('--maf_threshold', default=0, type=np.float64, help='Include only genotypes with minor allele frequency >= maf_threshold. Default: 0')
    parser.add_argument('--maf_threshold_interaction', default=0.05, type=np.float64, help='MAF threshold for interactions, applied to lower and upper half of samples')
    parser.add_argument('--dosages', action='store_true', help='Load dosages instead of genotypes (only applies to PLINK2 bgen input).')
    parser.add_argument('--return_dense', action='store_true', help='Return dense output for trans-QTL.')
    parser.add_argument('--return_r2', action='store_true', help='Return r2 (only for sparse trans-QTL output)')
    parser.add_argument('--best_only', action='store_true', help='Only write lead association for each phenotype (interaction mode only)')
    parser.add_argument('--output_text', action='store_true', help='Write output in txt.gz format instead of parquet (trans-QTL mode only)')
    parser.add_argument('--batch_size', type=int, default=20000, help='Batch size. Reduce this if encountering OOM errors.')
    parser.add_argument('--invnorm', action='store_true', default=False, help='Inverse normalize phenotypes after covariate correction. Not implemented for when using --interaction.')
    parser.add_argument('--load_split', action='store_true', help='Load genotypes into memory separately for each chromosome.')
    parser.add_argument('--disable_beta_approx', action='store_true', help='Disable Beta-distribution approximation of empirical p-values (not recommended).')
    parser.add_argument('--warn_monomorphic', action='store_true', help='Warn if monomorphic variants are found.')
    parser.add_argument('--fdr', default=0.05, type=np.float64, help='FDR for cis-QTLs')
    parser.add_argument('--qvalue_lambda', default=None, type=np.float64, help='lambda parameter for pi0est in qvalue.')
    parser.add_argument('--seed', default=None, type=int, help='Seed for permutations.')
    parser.add_argument('-o', '--output_dir', default='.', help='Output directory')
    args = parser.parse_args()

    # check inputs
    if args.mode == 'cis_independent' and (args.cis_output is None or not os.path.exists(args.cis_output)):
        raise ValueError("Output from 'cis' mode must be provided.")
    if args.interaction is not None and args.mode not in ['cis_nominal', 'trans']:
        raise ValueError("Interactions are only supported in 'cis_nominal' or 'trans' mode.")
    if args.interaction is not None and args.invnorm:
        raise NotImplementedError('--invnorm is not supported when using --interaction')

    logger = SimpleLogger(os.path.join(args.output_dir, f'{args.prefix}.tensorQTL.{args.mode}.log'))
    logger.write(f'[{datetime.now().strftime("%b %d %H:%M:%S")}] Running TensorQTL: {args.mode.split("_")[0]}-QTL mapping')
    if torch.cuda.is_available():
        logger.write(f'  * using GPU ({torch.cuda.get_device_name(torch.cuda.current_device())})')
    else:
        logger.write('  * WARNING: using CPU!')
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if args.seed is not None:
        logger.write(f'  * using seed {args.seed}')

    # load inputs
    logger.write(f'  * reading phenotypes ({args.phenotype_bed})')
    phenotype_df, phenotype_pos_df = read_phenotype_bed(args.phenotype_bed)
    # make sure TSS/cis-window is properly defined -- TODO: change to allow [start-w, end+w] windows
    assert phenotype_pos_df.columns[1] == 'pos', "The BED file must define the TSS/cis-window center, with start+1 == end."
    phenotype_pos_df.columns = ['chr', 'pos']
    pos_dict = phenotype_pos_df.T.to_dict()

    if args.covariates is not None:
        logger.write(f'  * reading covariates ({args.covariates})')
        covariates_df = pd.read_csv(args.covariates, sep='\t', index_col=0).T
        assert phenotype_df.columns.equals(covariates_df.index)
    else:
        covariates_df = None

    if args.paired_covariate is not None:
        assert covariates_df is not None, f"Covariates matrix must be provided when using paired covariate"
        paired_covariate_df = pd.read_csv(args.paired_covariate, sep='\t', index_col=0)  # phenotypes x samples
        assert paired_covariate_df.index.isin(phenotype_df.index).all(), f"Paired covariate phenotypes must be present in phenotype matrix."
        assert paired_covariate_df.columns.equals(phenotype_df.columns), f"Paired covariate samples must match samples in phenotype matrix."
    else:
        paired_covariate_df = None

    if args.interaction is not None:
        logger.write(f'  * reading interaction term(s) ({args.interaction})')
        # allow headerless input for single interactions
        with open(args.interaction) as f:
            f.readline()
            s = f.readline().strip()
        if len(s.split('\t')) == 2:  # index + value
            interaction_df = pd.read_csv(args.interaction, sep='\t', index_col=0, header=None)
        else:
            interaction_df = pd.read_csv(args.interaction, sep='\t', index_col=0)
        # select samples
        assert covariates_df.index.isin(interaction_df.index).all()
        interaction_df = interaction_df.loc[covariates_df.index].astype(np.float32)
    else:
        interaction_df = None

    if args.maf_threshold is None:
        if args.mode == 'trans':
            maf_threshold = 0.05
        else:
            maf_threshold = 0
    else:
        maf_threshold = args.maf_threshold

    if args.phenotype_groups is not None:
        group_s = pd.read_csv(args.phenotype_groups, sep='\t', index_col=0, header=None).squeeze('columns')
        # verify sort order
        group_dict = group_s.to_dict()
        previous_group = ''
        parsed_groups = 0
        for i in phenotype_df.index:
            if group_dict[i] != previous_group:
                parsed_groups += 1
                previous_group = group_dict[i]
        if not parsed_groups == len(group_s.unique()):
            raise ValueError('Groups defined in input do not match phenotype file (check sort order).')
    else:
        group_s = None

    # load genotypes
    if not args.load_split or args.mode != 'cis_nominal':  # load all genotypes into memory
        logger.write(f'  * loading genotype dosages' if args.dosages else f'  * loading genotypes')
        genotype_df, variant_df = genotypeio.load_genotypes(args.genotype_path, select_samples=phenotype_df.columns, dosages=args.dosages)
        if variant_df is None:
            assert not args.mode.startswith('cis'), f"Genotype data without variant positions is only supported for mode='trans'."

    if args.mode.startswith('cis'):
        if args.mode == 'cis':
            res_df = cis.map_cis(genotype_df, variant_df, phenotype_df, phenotype_pos_df, covariates_df=covariates_df,
                                 group_s=group_s, paired_covariate_df=paired_covariate_df, nperm=args.permutations,
                                 window=args.window, beta_approx=not args.disable_beta_approx, maf_threshold=maf_threshold,
                                 warn_monomorphic=args.warn_monomorphic, logger=logger, seed=args.seed, verbose=True, inverse_normal_transform=args.invnorm)
            logger.write('  * writing output')
            if has_rpy2:
                calculate_qvalues(res_df, fdr=args.fdr, qvalue_lambda=args.qvalue_lambda, logger=logger)
            out_file = os.path.join(args.output_dir, f'{args.prefix}.cis_qtl.txt.gz')
            res_df.to_csv(out_file, sep='\t', float_format='%.6g')
        elif args.mode == 'cis_nominal':
            if not args.load_split:
                cis.map_nominal(genotype_df, variant_df, phenotype_df, phenotype_pos_df, args.prefix, covariates_df=covariates_df,
                                interaction_df=interaction_df, maf_threshold_interaction=args.maf_threshold_interaction,
                                group_s=None, window=args.window, maf_threshold=maf_threshold, run_eigenmt=True,
                                output_dir=args.output_dir, write_top=True, write_stats=not args.best_only, logger=logger, verbose=True, inverse_normal_transform=args.invnorm)
                # compute significant pairs
                if args.cis_output is not None:
                    cis_df = pd.read_csv(args.cis_output, sep='\t', index_col=0)
                    nominal_prefix = os.path.join(args.output_dir, f'{args.prefix}.cis_qtl_pairs')
                    signif_df = get_significant_pairs(cis_df, nominal_prefix, group_s=group_s, fdr=args.fdr)
                    signif_df.to_parquet(os.path.join(args.output_dir, f'{args.prefix}.cis_qtl.signif_pairs.parquet'))

            else:  # load genotypes for each chromosome separately
                # currently only supports PLINK1.9 inputs, TODO
                pr = genotypeio.PlinkReader(args.genotype_path, select_samples=phenotype_df.columns, dtype=np.int8)
                top_df = []
                for chrom in pr.chrs:
                    g, pos_s = pr.get_region(chrom)
                    genotype_df = pd.DataFrame(g, index=pos_s.index, columns=pr.fam['iid'])[phenotype_df.columns]
                    variant_df = pr.bim.set_index('snp')[['chrom', 'pos']]
                    chr_df = cis.map_nominal(genotype_df, variant_df[variant_df['chrom'] == chrom],
                                             phenotype_df[phenotype_pos_df['chr'] == chrom], phenotype_pos_df[phenotype_pos_df['chr'] == chrom],
                                             args.prefix, covariates_df=covariates_df,
                                             interaction_df=interaction_df, maf_threshold_interaction=args.maf_threshold_interaction,
                                             group_s=None, window=args.window, maf_threshold=maf_threshold, run_eigenmt=True,
                                             output_dir=args.output_dir, write_top=True, write_stats=not args.best_only, logger=logger, verbose=True, inverse_normal_transform=args.invnorm)
                    top_df.append(chr_df)
                if interaction_df is not None:
                    top_df = pd.concat(top_df)
                    top_df.to_csv(os.path.join(args.output_dir, f'{args.prefix}.cis_qtl_top_assoc.txt.gz'),
                                  sep='\t', float_format='%.6g')

        elif args.mode == 'cis_independent':
            summary_df = pd.read_csv(args.cis_output, sep='\t', index_col=0)
            summary_df.rename(columns={'minor_allele_samples':'ma_samples', 'minor_allele_count':'ma_count'}, inplace=True)
            res_df = cis.map_independent(genotype_df, variant_df, summary_df, phenotype_df, phenotype_pos_df, covariates_df,
                                         group_s=group_s, fdr=args.fdr, nperm=args.permutations, window=args.window,
                                         maf_threshold=maf_threshold, logger=logger, seed=args.seed, verbose=True, inverse_normal_transform=args.invnorm)
            logger.write('  * writing output')
            out_file = os.path.join(args.output_dir, f'{args.prefix}.cis_independent_qtl.txt.gz')
            res_df.to_csv(out_file, sep='\t', index=False, float_format='%.6g')

        elif args.mode == 'cis_susie':
            if args.cis_output.endswith('.parquet'):
                signif_df = pd.read_parquet(args.cis_output)
            else:
                signif_df = pd.read_csv(args.cis_output, sep='\t')
            if 'qval' in signif_df:  # otherwise input is from get_significant_pairs
                signif_df = signif_df[signif_df['qval'] <= args.fdr]
            ix = phenotype_df.index[phenotype_df.index.isin(signif_df['phenotype_id'].unique())]
            summary_df, res = susie.map(genotype_df, variant_df, phenotype_df.loc[ix], phenotype_pos_df.loc[ix],
                                        covariates_df, L=args.L, paired_covariate_df=paired_covariate_df, maf_threshold=maf_threshold,
                                        max_iter=500, window=args.window, summary_only=False, inverse_normal_transform=args.invnorm)
            summary_df.to_parquet(os.path.join(args.output_dir, f'{args.prefix}.SuSiE_summary.parquet'))
            with open(os.path.join(args.output_dir, f'{args.prefix}.SuSiE.pickle'), 'wb') as f:
                pickle.dump(res, f)

    elif args.mode == 'trans':
        return_sparse = not args.return_dense
        pval_threshold = args.pval_threshold
        if pval_threshold is None and return_sparse:
            pval_threshold = 1e-5
            logger.write(f'  * p-value threshold: {pval_threshold:.2g}')

        if interaction_df is not None:
            if interaction_df.shape[1] > 1:
                raise NotImplementedError('trans-QTL mapping currently only supports a single interaction.')
            else:
                interaction_df = interaction_df.squeeze('columns')

        pairs_df = trans.map_trans(genotype_df, phenotype_df, covariates_df=covariates_df, interaction_s=interaction_df,
                                  return_sparse=return_sparse, pval_threshold=pval_threshold,
                                  maf_threshold=maf_threshold, batch_size=args.batch_size,
                                  return_r2=args.return_r2, logger=logger, inverse_normal_transform=args.invnorm)

        if variant_df is not None:
            logger.write('  * filtering out cis-QTLs (within +/-5Mb)')
            pairs_df = trans.filter_cis(pairs_df, pos_dict, variant_df, window=5000000)

        if args.permutations > 0:
            logger.write('  * Running permutations')
            permutations = trans.map_permutations(genotype_df, covariates_df, permutations=None,
                            chr_s=None, nperms=args.permutations, maf_threshold=maf_threshold,
                            batch_size=args.batch_size, logger=logger, seed=args.seed, verbose=True, inverse_normal_transform=args.invnorm)
            out_file = os.path.join(args.output_dir, args.prefix+'.permutations.txt.gz')
            permutations.to_csv(out_file, sep='\t', index=True, float_format='%.6g')
            pickle_out_file = os.path.join(args.output_dir, args.prefix+'.permutations.pickle')
            with open(pickle_out_file, 'wb') as f:
                pickle.dump(permutations, f)

        logger.write('  * writing output')
        if not args.output_text:
            pairs_df.to_parquet(os.path.join(args.output_dir, f'{args.prefix}.trans_qtl_pairs.parquet'))
        else:
            out_file = os.path.join(args.output_dir, f'{args.prefix}.trans_qtl_pairs.txt.gz')
            pairs_df.to_csv(out_file, sep='\t', index=False, float_format='%.6g')

    logger.write(f'[{datetime.now().strftime("%b %d %H:%M:%S")}] Finished mapping')


if __name__ == '__main__':
    main()
