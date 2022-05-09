#!/usr/bin/env/python3
import argparse
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from collections import defaultdict
import csv
import json
import pandas as pd


def rccs_lookup(rccs_json, resType, atomType, ph):
    with open(rccs_json) as jsp:
        rccs = json.load(jsp)

    if resType in rccs.keys():
        if 'PH' in rccs[resType].keys():
            if ph < rccs[resType]['PH']:
                if atomType in rccs[resType]['OXD'].keys():
                    return rccs[resType]['OXD'][atomType]
                else:
                    return None
            else:
                if atomType in rccs[resType]['RED'].keys():
                    return rccs[resType]['RED'][atomType]
                else:
                    return None
        else:
            if atomType in rccs[resType].keys():
                return rccs[resType][atomType]
            else:
                return None
    else:
        return None

def bmrbListDict_toDictDictList(listDict):
    dct = defaultdict(lambda: defaultdict(list))
    for e in listDict:
        dct[e['Comp_ID']][e['Atom_ID']].append(float(e['Val']))
    return dct


def afCSPdict_to_csAtomDct(afCSPdict):
    csp_atomList = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for afID in afCSPdict:
        for cspID in afCSPdict[afID]:
            try:
                for i, resType in enumerate(afCSPdict[afID][cspID]['residue_type']):
                    csp_atomList[resType][afCSPdict[afID][cspID]['atom'][i].upper()][cspID].append(
                        afCSPdict[afID][cspID]['chemical_shift'][i])
            except KeyError:
                pass
    return csp_atomList


def midpoint(p1, p2):
    return (p1 + p2) / 2


def listPts_listMidPts(bins):
    midptList = []
    for i in np.arange(bins.__len__() - 1):
        midptList.append(midpoint(bins[i], bins[i + 1]))
    return midptList


def calc_LeftTailWidth(count, auc, totalCount):
    intArea = 0
    binNum = 0
    while intArea < auc:
        intArea += count[binNum] / totalCount
        binNum += 1
    return binNum - 1


def calc_RightTailWidth(count, auc, totalCount):
    intArea = 0
    binNum = np.size(count)
    while intArea < auc:
        intArea += count[binNum - 1] / totalCount
        binNum -= 1
    return binNum + 1


def calc_binArray(csDict, tailAUC, binCount, bmrbDict=None):
    minVal = None
    maxVal = None

    for cspID in csDict:
        try:
            temp_minVal = min(csDict[cspID])
            temp_maxVal = max(csDict[cspID])
        except ValueError:
            continue
        if minVal:
            minVal = min(temp_minVal, minVal)
        else:
            minVal = temp_minVal
        if maxVal:
            maxVal = max(temp_maxVal, maxVal)
        else:
            maxVal = temp_maxVal

    if bmrbDict:
        minVal = min(min(bmrbDict), minVal)
        maxVal = max(max(bmrbDict), maxVal)

    binMin = None
    binMax = None

    try:
        temp_binArray = np.linspace(minVal, maxVal, binCount ** 2 + 1)
    except TypeError:
        return np.empty([])

    for cspID in csDict:
        tempArray = np.array(csDict[cspID])
        predArray = tempArray[np.isfinite(tempArray)]
        try:
            count, division = np.histogram(predArray, bins=temp_binArray)
        except ValueError:
            continue

        tempBinMin = calc_LeftTailWidth(count=count, auc=tailAUC, totalCount=np.sum(count))
        if binMin:
            binMin = min(binMin, tempBinMin)
        else:
            binMin = tempBinMin

        tempBinMax = calc_RightTailWidth(count=count, auc=tailAUC, totalCount=np.sum(count))
        if binMax:
            binMax = max(binMax, tempBinMax)
        else:
            binMax = tempBinMax

    if bmrbDict:
        bmrb_tempArray = np.array(bmrbDict)
        bmrb_predArray = bmrb_tempArray[np.isfinite(bmrb_tempArray)]
        bmrb_count, bmrb_division = np.histogram(bmrb_predArray, bins=temp_binArray)
        tempBinMin = calc_LeftTailWidth(count=bmrb_count, auc=tailAUC, totalCount=np.sum(bmrb_count))
        tempBinMax = calc_RightTailWidth(count=bmrb_count, auc=tailAUC, totalCount=np.sum(bmrb_count))

        if binMin:
            binMin = min(binMin, tempBinMin)
        else:
            binMin = tempBinMin

        if binMax:
            binMax = max(binMax, tempBinMax)
        else:
            binMax = tempBinMax

    binArray = np.linspace(temp_binArray[binMin], temp_binArray[binMax], binCount + 1)
    return binArray


def distributionCSP(bmrbCSV, bmrbCS, allCS, rccs_jsonFile):
    with open(allCS) as jsp:
        predictions = json.load(jsp)

    csPredictor_list = [{"id": 1, "csp_name": "sparta_plus"}, {"id": 2, "csp_name": "shiftx2"}, {"id": 3, "csp_name": "larmor_ca"}, {"id": 4, "csp_name": "rcs"}, {"id": 5, "csp_name": "shifts"}, {"id": 6, "csp_name": "cheshift"}, {"id": 8, "csp_name": "ucbshift"}]
    # with open('/local/PycharmProjects/CSPworkflow/code/usage/all_csPredictorList.json') as jsp2:
    #     csPredictor_list = json.load(jsp2)

    bmrbDict = defaultdict(lambda: defaultdict(lambda: defaultdict()))
    with open(bmrbCSV) as fp:
        reader = csv.reader(fp, delimiter=",", quotechar='"')
        next(reader)  # starting line is empty
        header = next(reader)
        if header != None:
            for row in reader:
                bmrbDict[row[0]][row[1]][header[2]] = row[2]
                bmrbDict[row[0]][row[1]][header[5]] = row[5]
                bmrbDict[row[0]][row[1]][header[6]] = row[6]

    with open(bmrbCS) as jsp:
        temp_bmrbCS = json.load(jsp)
    bmrbDictCS = bmrbListDict_toDictDictList(listDict=temp_bmrbCS)

    # TODO: Create inputs into definition to pass paths in
    csp_atomList = afCSPdict_to_csAtomDct(afCSPdict=predictions)

    plt.ioff()
    x_ticks_labels = []
    x_ticks = []
    for csp_id in csPredictor_list:
        x_ticks_labels.append(csp_id['csp_name'])
        x_ticks.append(csp_id['id'])

    cspIDList = defaultdict(str)
    for cspEntry in csPredictor_list:
        cspIDList[cspEntry['id']] = cspEntry['csp_name']

    colors = plt.cm.gist_earth(np.linspace(0, 1, cspIDList.__len__() * 10))
    colorDict = {cspID: colors[i * 10] for i, cspID in enumerate(cspIDList.keys())}

    lines_array = ['-.', '-', ':'] * 3
    lineStyleDict = {cspID: lines_array[i] for i, cspID in enumerate(cspIDList.keys())}

    for resType in csp_atomList:
        for atomType in csp_atomList[resType]:
            ymax = 0
            fig, ax = plt.subplots(1, 1)
            fig.subplots_adjust(right=0.75)
            binCount = 100
            plt.xlabel('Chemical Shift [ppm]', fontsize=10)
            plt.ylabel('Normalized Percent', fontsize=10)
            binArray = calc_binArray(csDict=csp_atomList[resType][atomType], binCount=binCount,
                                     bmrbDict=bmrbDictCS[resType][atomType], tailAUC=0.015)

            if binArray.size == 0:
                continue
            for cspID in csp_atomList[resType][atomType]:
                tempArray = np.array(csp_atomList[resType][atomType][cspID])
                predArray = tempArray[np.isfinite(tempArray)]
                try:
                    count, division = np.histogram(predArray, bins=binArray)
                except ValueError:
                    print(resType, atomType)
                    continue
                countHist = pd.DataFrame(count, columns=['count']).rolling(3).mean()
                totalCount = np.sum(count)
                midPts = np.asarray(listPts_listMidPts(division))

                if ymax < np.max(countHist / totalCount)['count']:
                    ymax = np.max(countHist / totalCount)['count']
                try:
                    ax.plot(midPts, np.array(countHist / totalCount).ravel() * 100,
                            label=f"{cspIDList[int(cspID)]} ({totalCount})", c=colorDict[int(cspID)],
                            linewidth=1, ls=lineStyleDict[int(cspID)])
                except ValueError:
                    print(resType, atomType)
                    continue

            bmrb_tempArray = np.array(bmrbDictCS[resType][atomType])
            bmrb_predArray = bmrb_tempArray[np.isfinite(bmrb_tempArray)]
            bmrb_count, bmrb_division = np.histogram(bmrb_predArray, bins=binArray)
            bmrb_countHist = pd.DataFrame(bmrb_count, columns=['count']).rolling(3).mean()
            bmrbCount = np.sum(bmrb_count)
            try:
                plt.fill_between(midPts, np.array(bmrb_countHist / bmrbCount).ravel() * 100, color='gray', alpha=0.3,
                                 label=f"BMRB ({bmrbCount})")
            except ValueError:
                continue

            rccsVal = rccs_lookup(rccs_json=rccs_jsonFile, resType=resType, atomType=atomType, ph=7)
            if rccsVal:
                ax.vlines(x=rccsVal, ymin=0, ymax=ymax * 100, colors='b', ls='--', lw=1, label=f"Random Coil")

            ax.legend(loc=(1.02, 0.15), prop={'size': 8})
            fig.savefig(f"{resType}_{atomType}.eps", format='eps', dpi=1200)
            plt.close('all')


def main():
    parser = argparse.ArgumentParser(description='You can add a description here')
    parser.add_argument('--bmrb_csv', required=True)
    parser.add_argument('--bmrbCS', required=True)
    parser.add_argument('--all_cs', required=True)
    parser.add_argument('--rccs_lookup', required=True)

    args = parser.parse_args()

    distributionCSP(bmrbCSV=args.bmrb_csv, bmrbCS=args.bmrbCS, allCS=args.all_cs, rccs_jsonFile=args.rccs_lookup)


if __name__ == '__main__':
    main()
