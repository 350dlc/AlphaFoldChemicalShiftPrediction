# AlphaFoldChemicalShiftPrediction

standalone_compareCSP.py requires the following arguments:
--bmrb_csv # Path to csv file containing the counts with range and basic statistics of depositions, on an amino acid and atom basis, to the BMRB as of 12/14/21
--bmrbCS   # Path to JSON file containg all chemical shift depositions on a per residue and atom basis to the BMRB as of 12/10/21 for single chain      proteins
--all_cs   # Path to JSON file containg all predicted chemical shifts on a per residue and atom basis generated using our CSP workflow
--rccs_lookup # Path to JSON file containing the random coil chemical shift depositions provided by Wishart, et al., 1H, 13C and 15N random coil NMR chemical shifts of the common amino acids. I. investigations of nearest-neighbor effects. J. biomolecular NMR 5, 67–81 (1995).
