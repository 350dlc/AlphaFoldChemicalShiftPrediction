import CifFile as cif
from resources.mmCIF_support import categoryName_loopNumber
import argparse
import os
import copy
import datetime
from collections import OrderedDict
import configparser
import psycopg2
import getpass
from pathlib import Path
from collections import defaultdict
from resources.get_id import bmrb2pdb_ID
import shlex


class ConfigObject:
    """
    Read a configparser file into a 'dot-able' object
    This class supports nested config files.
    """

    def __init__(self, file=None, list_delimiter=',', nested_section_name='configs'):

        """
        Using the dot to access fields and nested fields only works for class objects that have self.__dict__

        In order to use this feature all the way through nested ConfigObjects, fill the top level of __dict__
        with OrderedDict (replaces default of regular dict) and fill all nested levels with ConfigObjects.

        Note: the keys put in __dict__ will appear as properties of ConfigObject, just like self.list_delimiter,
        but ConfigObject.keys() can be used to only return the keys from __dict__
        """
        self.__dict__ = OrderedDict()
        self.list_delimiter = list_delimiter

        if not file:
            # allow constructor to be called with no file so that empty ConfigObject is created and can be populated
            return

        if not os.path.isfile(file):
            raise FileExistsError('config file not found: {:s}'.format(file))

        # read master config file
        cparser = configparser.ConfigParser(interpolation=None)
        cparser.optionxform = str
        cparser.read(file)

        for sec in cparser.sections():
            if sec == nested_section_name:
                # found the keyword that indicates this section contains config files that should be parsed
                for param in cparser.options(sec):
                    # get the filename and perform system variable substitution (i.e. allow $HOME in config file)
                    config_file = os.path.expandvars(cparser.get(sec, param))
                    if not config_file.startswith('/'):
                        # if filename is not absolute => assume filename is relative to location of main config file
                        config_file = os.path.join(os.path.dirname(file), config_file)
                    if not os.path.isfile(config_file):
                        raise FileExistsError('config file not found: {:s}'.format(config_file))
                    # parse the config file and insert ConfigObject into self
                    self.__dict__[param] = ConfigObject(
                        file=config_file,
                        list_delimiter=self.list_delimiter,
                    )
            else:
                # create a new section as an empty ConfigObject
                self.__dict__[sec] = ConfigObject(
                    file=None,
                    list_delimiter=list_delimiter,
                )
                # fill this new section with key/value pairs
                for param in cparser.options(sec):
                    self.__dict__[sec].__dict__[param] = self.format_val(cparser.get(sec, param))

    def get(self, sec, param=None):
        """
        Return the portion of the object rooted at the given key
        """
        if param:
            try:
                return self.__dict__[sec].__dict__[param]
            except KeyError:
                raise KeyError('parameter not found: {:s}/{:s}'.format(sec, param))
        else:
            try:
                return self.__dict__[sec]
            except KeyError:
                raise KeyError('section not found: {:s}'.format(sec))

    def set(self, sec, param=None, value=None):
        """
        Set the value for the given key
        """
        if param:
            try:
                self.__dict__[sec].__dict__[param] = value
            except KeyError:
                raise KeyError('parameter not found: {:s}/{:s}'.format(sec, param))
        else:
            try:
                self.__dict__[sec] = value
            except KeyError:
                raise KeyError('section not found: {:s}'.format(sec))

    def dictionary(self):
        """
        Recursively unpack nested ConfigObject into nested dictionary
        :return: dictionary
        """
        d = dict()
        for key in self.__dict__:
            if isinstance(self.__dict__[key], ConfigObject):
                d[key] = self.__dict__[key].dictionary()
            else:
                d[key] = self.__dict__[key]
        return d

    def keys(self):
        """
        Return all keys at top level of ConfigObject
        :return: list
        """
        return list(self.__dict__.keys())

    def format_val(self, val):
        """
        Convert a value (given as str) into correct data type and store as list, if applicable
        :param val: str input
        :return: list
        """
        # convert value to a list
        if isinstance(val, str):
            val_list = val.split(self.list_delimiter)
            val_list = [v.strip() for v in val_list]
        else:
            if not isinstance(val, list):
                val_list = [val]
            else:
                val_list = val

        num_val_in = len(val_list)
        val_out = []

        for v in val_list:
            if v in ['true', 'True']:
                val_out.append(True)
            elif v in ['false', 'False']:
                val_out.append(False)
            else:
                for t in [int, float, str]:
                    try:
                        val_out.append(list(map(t, [v]))[0])
                        break
                    except ValueError:
                        pass  # try next format option

        if len(val_out) != num_val_in:
            raise ValueError('failed to convert cfg value: {:s}'.format(val))
        else:
            if len(val_out) == 1:
                return val_out[0]
            else:
                return val_out

    def print(self, OBJ=None, level=0, param_length=25):
        """
        Print the sections, parameters, and values.  Use indenting to indicate nesting.
        :param OBJ: object to print (used to accept recursive call for nested configs)
        :param level: current nesting level - used to control indentation
        :param key_length: field width to use when printing parameter names
        :return:
        """
        # indent the keys progressively with nested sections
        pad = '    ' * level
        # indent multiline values (the last 2 spaces account for "= " and ensure that trailing lines are at same depth)
        multiline_pad = ' ' * key_length + pad + '  '

        if not OBJ:
            OBJ = self.__dict__

        for k in OBJ.keys():
            if isinstance(OBJ.get(k), ConfigObject):
                print('-' * 40 + '\n' + pad + '[{}]'.format(k))
                self.print(OBJ=OBJ.get(k), level=level + 1, param_length=param_length)
            else:
                val = OBJ.get(k)
                if isinstance(val, str):
                    val = val.replace('\n', '\n' + multiline_pad)

                print(pad + '{key:{key_length}s}= {val:}'.format(
                    key=k, key_length=key_length, val=val))


class postgreSQL:
    def __init__(self, cinfo, database, pw=None):

        self.application_name = 'nusforall'

        self.conn = self.connect(cinfo, database, pw)

    def connect(self, cinfo, database, pw=None):
        """
        Connect to database, set schema if present, return connection
        :param cinfo: connection info (section in config file) containing host, host_local, username
        :param database: name of the database
        :param pw: password for username specified in cinfo
        :return: connection
        """

        try:
            h = cinfo.host
            u = cinfo.get('username', None)
            # if not u:
            #     u = pwd.getpwuid(os.getuid()).pw_name
            # pwc = password_cache.PasswordCache(self.config)
            # p = pwc.password(u, Database.__DATABASE)
            if pw is None:
                pw = getpass.getpass('password for {} on {}:'.format(u, h))

        except KeyError as ke:
            print(
                "missing data, check config section {} for {}".format(DB_KEY, ke))
            raise

        try:
            conn = psycopg2.connect(
                host=h,
                dbname=database,
                user=u,
                password=pw,
                application_name=self.application_name)
        except psycopg2.OperationalError:
            try:
                h = cinfo.host_local
                conn = psycopg2.connect(
                    host=h,
                    dbname=database,
                    user=u,
                    password=pw,
                    application_name=self.application_name)
            except psycopg2.OperationalError:
                # pwc.reject_password(u, Database.__DATABASE)
                raise
        return conn

    def query(self, q):
        """
        Run query against connected SQL database
        :param q: SQL query
        :return: returned data from DB
        """

        # execute query and return data
        cur = self.conn.cursor()
        cur.execute(q)
        self.conn.commit()
        try:
            return cur.fetchall()
        except:
            return None


class queryData:
    """
    Class for running sql queries against db and returning result
    """

    def __init__(self, cfg, basename, subs=None):

        """
        Run a query against a database and perform substitution into the query
        :param basename: basename of the query defined in the config file (config file has sections named 'Q_'+basename
        :param subs: dictionary of substitutions to make into query string (keys=string to replace, value=string to insert)
        :return: result of query and a format string for display
        """
        self.cfg = cfg
        try:
            # create a copy of the template so that variable substitution can be made without
            # overwriting the template
            qSection_name = 'Q_' + basename
            qSection = copy.copy(self.cfg.get(qSection_name))
        except KeyError:
            raise KeyError('query not found: {:s}'.format(qSection_name))

        try:
            dbSection_name = qSection.get('database')
            dbSection = self.cfg.get(dbSection_name)
        except KeyError:
            raise KeyError('database not found: {:s}'.format(dbSection_name))

        # modify the query by substituting substitution values
        if subs is not None:
            for pattern in subs:
                if basename == 'protInsert':
                    argument_string = ",".join(
                        "('%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s')" % (
                            a, b, c, d, e, f, g, h, i, j, k, l) for (a, b, c, d, e, f, g, h, i, j, k, l) in
                        subs[pattern])
                    qSection.query = qSection.query.replace(pattern, argument_string)
                elif basename == 'insertCSList':
                    argument_string = ",".join(
                        "('%s', '%s', '%s', '%s', '%s', '%s', '%s')" % (
                            a, b, c, d, e, f, g) for (a, b, c, d, e, f, g) in subs[pattern])
                    qSection.query = qSection.query.replace(pattern, argument_string)
                else:
                    qSection.query = qSection.query.replace(pattern, str(subs[pattern]))

        # get connection
        conn = self.get_conn(dbSection=dbSection)

        # run query and capture results as a list of tuples (one for each row in the table)
        qTuple = conn.query(qSection.query)

        self.qHeader = qSection.header
        self.qFormat = qSection.format

        if not isinstance(self.qFormat, list):
            # queries with 1 column will have a format that is a string => encapsulate in list
            self.qFormat = [self.qFormat]
        if not isinstance(self.qHeader, list):
            # queries with 1 column will have a header that is a string => encapsulate in list
            self.qHeader = [self.qHeader]

        # iterate through list of tuples and put each into a dictionary, using header names as keys
        self.data = []
        if qTuple is not None:
            for tup in qTuple:
                row_dict = {}
                for key, val in zip(self.qHeader, tup):
                    if isinstance(val, str):
                        # replace all non-ASCII characters with a "?"
                        # just for printed output - no changes made to database
                        val = val.encode('ascii', 'replace').decode('ascii')
                    row_dict[key] = val
                self.data.append(row_dict)

    def get_conn(self, dbSection):
        """
        Create a connection to a database server
        :param dbSection: section from cfg file with entries: host, host_local, username, dbname
        :return: postgreSQL object
        """
        # search the .pgpass file for local user to retrieve password for database connection
        # TODO: Create env var for PGPASS
        # pw = pgpasslib.getpass(
        #     host=dbSection.get('host'),
        #     dbname=dbSection.get('dbname'),
        #     user=dbSection.get('username'))
        pw = PASSWORD
        if pw is None:
            print('unable to retrieve password from .pgpass file')
            pw = input('Please enter password: ')

        # create connection
        conn = postgreSQL(
            cinfo=dbSection,
            database=dbSection.dbname,
            pw=pw,
        )
        return conn

    def get(self, col, forceList=False):
        """
        Return the value(s) for the named column.
        If table has a single row, return the value.
        If table has multiple rows, return a list of values (one for each row).
        This function should replace getCol
        :param col: name of the column
        :param forceList: force the output to be returned as a list, even for single values
        :return: value(s)
        """
        if col not in self.qHeader:
            raise ValueError('Column not found: {:s}'.format(col))
        valList = [row[col] for row in self.data]

        if (not forceList) and (len(valList) == 1):
            valList = valList[0]

        return valList

    def reduce(self, keep):
        """
        Reduce the queryData to only include fields specified in keep
        :param keep: list of fields to keep
        :return:
        """
        bad = [k for k in keep if k not in self.qHeader]
        if bad:
            print(bad)
            raise ValueError('Asking for data values not in query results.')

        # get the index values of all fields in keep as they appear in QueryData object
        i_keep = [self.qHeader.index(k) for k in keep if k in self.qHeader]
        # reduce the header and format
        self.qHeader = [self.qHeader[i] for i in i_keep]
        self.qFormat = [self.qFormat[i] for i in i_keep]

        for rDict in self.data:
            for key in list(rDict):
                if key not in keep:
                    del rDict[key]
        return self

    def qFormat2str_only(self):
        """
        Convert the qFormat string into one that only uses string types - intended for
        printing a table header using the same field withs as the row data
        :return: format template
        """
        # replace numeric formats with strings, so that same layout can be used to print header
        return [f.replace('d', 's').replace('f', 's').replace(',', '') for f in self.qFormat]

    def print(self, mode='table', indent=0):
        """
        Print the rows of the data table using format template
        :param mode: specify if 'table' should be printed or list of key=value pairs
        :param indent: number of space to indent the printed lines
        :return:
        """
        #  validate that number of header labels matches number of substitutions in format string and
        #  matches number of fields in data
        if self.numCols() == 0:
            print('\nNo data\n')
            return
        if not (len(self.qFormat) == len(self.qHeader) == self.numCols()):
            raise ValueError('format / header / data size mismatch')

        if mode == 'table':
            # use qFormat to build a format string for printing data
            format_data = ' ' * indent + ' | '.join(self.qFormat)
            # use qFormat to build a format string for printing header (str type only)
            format_header = ' ' * indent + ' | '.join(self.qFormat2str_only())
            header = format_header.format(*self.qHeader)

            print('-' * len(header))
            print(header)
            print('-' * len(header))
        elif mode == 'kv':
            # determine the length of the longest key so that printed output can be set to fit
            k_width = max([len(k) for k in self.qHeader])

        for row in self.data:
            # iterate through keywords defined by header and assemble corresponding values
            # into a list (make format conversions as needed)
            d_fix = []
            for key in self.qHeader:
                val = row[key]
                # modify the raw value if needed and put into list for printing
                if val is None:
                    d_fix.append('')
                elif isinstance(val, datetime.datetime):
                    # convert datetime object to a nicely formatted string
                    d_fix.append(val.strftime("%Y %b %d, %I%p"))
                elif isinstance(val, datetime.date):
                    d_fix.append(str(val))
                elif isinstance(val, bool):
                    d_fix.append(str(val))
                else:
                    d_fix.append(val)

            if len(d_fix) != len(self.qHeader):
                raise ValueError('query table and header size mismatch')

            if mode == 'table':
                # print the modified values using the format template
                print(format_data.format(*d_fix))
            elif mode == 'kv':
                # print key = value
                for key, val in zip(self.qHeader, d_fix):
                    print(' ' * indent + '{key:{k_width:d}s} = {val:s}'.format(key=key, val=str(val), k_width=k_width))

    def count(self):
        """Number of rows (i.e. data entries) in the table"""
        return len(self.data)

    def numCols(self):
        """Number of columns (i.e. fields) in the table"""
        if self.data:
            return len(self.data[0].keys())
        else:
            return 0

    def fields(self):
        return self.qHeader


class timedomain():

    def __init__(self, cfgFile):
        base = os.path.dirname(os.path.abspath(__file__))
        self.cfg = ConfigObject(
            file=os.path.join(base, 'usage', 'configs', cfgFile),
            list_delimiter='&')

    def query(self, basename, subs=None):
        """
        Run a query against a database and perform substitution into the query
        :param basename: basename of the query defined in the cfg file (cfg file has sections named 'Q_'+basename
        :param subs: dictionary of substitutions to make into query string (keys=string to replace, value=string to insert)
        :return: result of query
        """
        try:
            return queryData(
                cfg=self.cfg,
                basename=basename,
                subs=subs)
        except Exception as e:
            print(e)


def augment_mmCIF(inputPath, outputPath):
    if os.path.isfile(inputPath):
        uniprot_id, af_id = reboxitoryPath_to_uniprotAF(inputPath)
        af_entry_id = check_for_cs_predictions(uniprot_id=uniprot_id, af_id=af_id)
        if af_entry_id:
            base = os.path.basename(inputPath)
            newBase = base.replace('.cif', '_augmented.cif')
            outputFile = os.path.join(outputPath, newBase)
            # try:
            #     os.remove(outputFile)
            # except OSError:
            #     pass
            checkFile = Path(outputFile)
            if checkFile.is_file():
                return
                # os.remove(outputFile)
            try:
                print_ascension_ids(augmented_cifFilename=outputFile, af_id=af_id, uniprot_id=uniprot_id)
                print_orig_cif(orig_cifFile=inputPath, augmented_cifFilename=outputFile, af_id=af_id)

                csDictionary = queeryCS_to_dictionary(af_entry_id)
                print_aug_atom_site(af_file=inputPath, csDict=csDictionary, augmented_csFilename=outputFile,
                                    af_id=af_entry_id, af_entry_name=af_id)
                print_software(csDict=csDictionary, augmented_csFilename=outputFile)
                print_authorList(augmented_csFilename=outputFile)
            except:
                print(af_entry_id)
                try:
                    os.remove(outputFile)
                except OSError:
                    return
    elif os.path.isdir(inputPath):
        listInputAF = searchPathExt(inputPath=inputPath)
        for afFile in listInputAF:
            augment_mmCIF(inputPath=afFile, outputPath=outputPath)


def check_for_cs_predictions(uniprot_id, af_id):
    try:
        td = timedomain(cfgFile=cfgFile)
        af_entry_id = td.query(
            basename='afID_Index',
            subs={
                "%%%GENOMEID%%%": uniprot_id,
                "%%%PROTEINID%%%": af_id
            }
        ).data[0]['id']
        return af_entry_id
    except IndexError:
        return False
    except AttributeError:
        return False


def searchPathExt(inputPath, extension='.cif'):
    extFileList = []
    for root, dirs, files in os.walk(inputPath):
        for file in files:
            if file.endswith(extension):
                extFileList.append(os.path.join(root, file))
    return extFileList


def reboxitoryPath_to_uniprotAF(reboxitoryPath):
    p = Path(reboxitoryPath)
    af_id = p.stem
    uniprot_id = p.parts[-2]
    return uniprot_id, af_id


def print_orig_cif(orig_cifFile, augmented_cifFilename, af_id):
    with open(orig_cifFile) as file:
        lines = file.readlines()

    afEntry = ('-').join(af_id.split('-')[:-1])
    line_number_start = find_line_number(lines=lines, string_to_parse=f"_entry.id {afEntry}") + 1
    line_number_cutoff = find_line_number(lines=lines, string_to_parse='_atom_site.group_PDB') -1

    # with open(augmented_cifFilename, mode='wt', encoding='utf-8') as myfile:
    with open(augmented_cifFilename, mode='a', encoding='utf-8') as myfile:
        myfile.write(''.join(lines[line_number_start:line_number_cutoff]))


def find_line_number(lines, string_to_parse):
    for line_num, line in enumerate(lines):
        if line.rstrip() == string_to_parse:
            return line_num


def count_residues(af_id):
    td = timedomain(cfgFile=cfgFile)
    maxResNum = td.query(
        basename='maxResNum',
        subs={
            "%%%AFID%%%": af_id
        }
    ).data[0]['residue_sequence']
    return maxResNum


def queeryCS_to_dictionary(af_id):
    csDict = defaultdict()

    td = timedomain(cfgFile=cfgFile)
    csp_id_dict = td.query(
        basename='selectUniqueAF_cspID',
        subs={
            '%%%AFID%%%': af_id}).data

    cspID_dict = filter_csps(uniqueList=csp_id_dict, filter=cspID_list)

    for id in cspID_dict:
        cs_pred = td.query(
            basename='compareCSP',
            subs={
                "%%%AFID%%%": af_id,
                "%%%CSPID%%%": id['csp_id']}
        ).data
        csDict[id['csp_id']] = listDict_to_DictList(listDict=cs_pred)
    return csDict


def listDict_to_DictList(listDict):
    dct = defaultdict(list)
    for e in listDict:
        for k in e.keys():
            dct[k].append(e[k])
    return dct


def filter_csps(uniqueList, filter):
    return [csp_id for csp_id in uniqueList if csp_id['csp_id'] in filter]


def query_afH_atoms(af_id, chain='A'):
    # TODO: Future multichain models will require adjustment here
    td = timedomain(cfgFile=cfgFile)
    afH_atoms = td.query(
        basename='select_pdbAtoms',
        subs={
            "%%%AFID%%%": af_id,
            "%%%CHAIN%%%": chain
        }
    ).data
    return afH_atoms


def searchDictDictList(dct, resNum, atom, cspID):
    atomIdx = None
    # indices = dct[cspID]['res_sequence'].index(resNum)
    indices = [i for i, x in enumerate(dct[cspID]['res_sequence']) if x == resNum]

    for i in indices:
        if dct[cspID]['atom'][i] == atom:
            atomIdx = i
    if atomIdx:
        return dct[cspID]['chemical_shift'][atomIdx]
    else:
        return None


def check_for_spaceDelimiter(filename, delimiter='#'):
    with open(filename, 'rb') as f:
        try:  # catch OSError in case of a one line file
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b'\n':
                f.seek(-2, os.SEEK_CUR)
        except OSError:
            f.seek(0)
        lastLine = f.readline().decode()

    if lastLine.rstrip() == delimiter:
        return True
    else:
        return False


# def print_loop_noVals(filename, loopList):
#     if check_for_spaceDelimiter(filename=filename)
#


def print_ascension_ids(augmented_cifFilename, uniprot_id, af_id):
    with open(mapping_file) as file:
        lines = file.readlines()

    bmrb_id = []
    for line in lines:
        af_file_path = line.rstrip().split(' ')[0]
        if af_file_path.split('/')[-1].strip('.pdb') == af_id:
            spline = line.rstrip().strip(af_file_path).split(',')

            for entry in spline:
                tmp_id = str()
                for e in list(filter(None, entry)):
                    try:
                        int(e)
                    except ValueError:
                        continue
                    tmp_id += e
                bmrb_id.append(tmp_id)
    bmrbID_list = list(set(bmrb_id))
    pdbID_list = []
    for bmrbID in bmrbID_list:
        pdbID_list.append(''.join(bmrb2pdb_ID(bmrbID)))

    # with open(augmented_cifFilename, 'a') as file:
    #     print(f"_ascension_ids.uniprot {uniprot_id}", file)
    #     print(f"_ascension_ids.bmrb {bmrbID_list}", file)
    #     print(f"_ascension_ids.pdb {pdbID_list}", file)

    afEntry = ('-').join(af_id.split('-')[:-1])

    fstring = []
    fstring.append(f"data_{afEntry}\n")
    fstring.append(f"#\n")
    fstring.append(f"_entry.id {afEntry}\n")
    fstring.append(f"#\n")
    fstring.append(f"loop\n")
    fstring.append(f"_ascension_ids.uniprot {uniprot_id}\n")
    fstring.append(f"_ascension_ids.bmrb    {' '.join(bmrbID_list)}\n")
    fstring.append(f"_ascension_ids.pdb     {' '.join(pdbID_list)}\n")
    with open(augmented_cifFilename, mode='a', encoding='utf-8') as myfile:
        myfile.write(''.join(fstring))

    return None


def print_loop_singleVal(filename, orderedDict):
    maxChar = len(max(list(orderedDict.keys()), key=len))
    with open(filename, 'a') as file:
        if not check_for_spaceDelimiter(filename=filename):
            print("#", file=file)
        print("loop_", file=file)
        for k, v in orderedDict.items():
            print(f"{k: <{maxChar + 3}} {v}", file=file)
        print("#", file=file)
    return None


def first(s):
    '''Return the first element from an ordered collection
       or an arbitrary element from an unordered collection.
       Raise StopIteration if the collection is empty.
    '''
    return next(iter(s))


def fstring_dictionary(val_dic, len_restrict_dict):
    list_string = []

    for k in val_dic:
        list_string.append(f"{val_dic[k]: <{len_restrict_dict[k] + 1}}")
    return_string = ' '.join(list_string)
    return return_string


def print_loop_multiVal(filename, orderedDict):
    dict_maxLen = dict()

    for cspID in orderedDict:
        for k, v in orderedDict[cspID].items():
            try:
                dict_maxLen[k] = max(dict_maxLen[k], str(v).__len__())
            except KeyError:
                dict_maxLen[k] = str(v).__len__()

    with open(filename, 'a') as file:
        if not check_for_spaceDelimiter(filename=filename):
            print("#", file=file)
        print("loop_", file=file)

        for k in orderedDict.keys():
            # maxChar = len(max(list(orderedDict[k].keys()), key=len))
            if k == first(orderedDict):
                for subDict_key in orderedDict[k].keys():
                    print(subDict_key, file=file)

            print(fstring_dictionary(val_dic=orderedDict[k], len_restrict_dict=dict_maxLen), file=file)



def print_protonation_loop(outputFile):
    # TODO: future versions need to be dynamic to include multiple software methods/versions
    odict = OrderedDict()
    odict['_protonation_method.idx'] = 1
    odict['_protonation_method.name'] = 'REDUCE'
    # odict['_protonation_method.version'] = 'reduce.4.7.210416'
    odict['_protonation_method.version'] = '4.7.210416'
    print_loop_singleVal(filename=outputFile, orderedDict=odict)
    return None


def print_csp_loop(outputFile, cspList):
    # TODO: future versions need to be dynamic to include multiple software methods/versions
    odict = defaultdict(lambda: OrderedDict())
    orederedDict = defaultdict(lambda: OrderedDict())

    odict[1]['_chemical_shift_predictor.idx'] = 1
    odict[2]['_chemical_shift_predictor.idx'] = 2
    odict[3]['_chemical_shift_predictor.idx'] = 3
    odict[4]['_chemical_shift_predictor.idx'] = 4
    odict[5]['_chemical_shift_predictor.idx'] = 5
    odict[6]['_chemical_shift_predictor.idx'] = 6
    odict[8]['_chemical_shift_predictor.idx'] = 8
    odict[1]['_chemical_shift_predictor.name'] = 'Sparta+'
    odict[2]['_chemical_shift_predictor.name'] = 'SHIFTX2'
    odict[3]['_chemical_shift_predictor.name'] = 'LarmorCA'
    odict[4]['_chemical_shift_predictor.name'] = 'RCS'
    odict[5]['_chemical_shift_predictor.name'] = 'SHIFTS'
    odict[6]['_chemical_shift_predictor.name'] = 'CheShift'
    odict[8]['_chemical_shift_predictor.name'] = 'UCBSHIFT'
    odict[1]['_chemical_shift_predictor.version'] = '\"2.70F1 Rev 2012.029.12.03\"'
    odict[2]['_chemical_shift_predictor.version'] = '\"Ver 1.10A\"'
    odict[3]['_chemical_shift_predictor.version'] = 'v1.00'
    odict[4]['_chemical_shift_predictor.version'] = '?'
    # TODO: determine RCS vesion and UCBSHIFT
    odict[5]['_chemical_shift_predictor.version'] = '\"Version 5.6\"'
    odict[6]['_chemical_shift_predictor.version'] = 'v3.6'
    odict[8]['_chemical_shift_predictor.version'] = '?'
    odict[1]['_chemical_shift_predictor.temperature'] = '.'
    odict[2]['_chemical_shift_predictor.temperature'] = '298'
    odict[3]['_chemical_shift_predictor.temperature'] = '.'
    odict[4]['_chemical_shift_predictor.temperature'] = '.'
    odict[5]['_chemical_shift_predictor.temperature'] = '.'
    odict[6]['_chemical_shift_predictor.temperature'] = '.'
    odict[8]['_chemical_shift_predictor.temperature'] = '.'
    odict[1]['_chemical_shift_predictor.ph'] = '.'
    odict[2]['_chemical_shift_predictor.ph'] = '7'
    odict[3]['_chemical_shift_predictor.ph'] = '.'
    odict[4]['_chemical_shift_predictor.ph'] = '.'
    odict[5]['_chemical_shift_predictor.ph'] = '.'
    odict[6]['_chemical_shift_predictor.ph'] = '.'
    odict[8]['_chemical_shift_predictor.ph'] = '7'

    for cspID in cspList:
        if cspID in list(odict.keys()):
            orederedDict[cspID] = odict[cspID]

    print_loop_multiVal(filename=outputFile, orderedDict=orederedDict)


def checkCategoryInLoop(loop, categoryName, delimiter='.'):
    category_name, attribute_name = loop[0].split(delimiter)
    if categoryName == category_name:
        return True


def print_atom_site_loop(outputFile, cspList, cifDict):
    loopNum = categoryName_loopNumber(loopsDict=cifDict.loops, categoryName='_atom_site')
    # TODO: dynamically determine the number of sets of protonated coordinates and chemical shift columns to include
    loop = cifDict.loops[loopNum]
    insert_index = loop.index("_atom_site.cartn_z") + 1
    loop.insert(insert_index, "_atom_site.cartn_x_protonated_1")
    loop.insert(insert_index + 1, "_atom_site.cartn_y_protonated_1")
    loop.insert(insert_index + 2, "_atom_site.cartn_z_protonated_1")

    insert_index = loop.index("_atom_site.cartn_z_protonated_1") + 1
    for cspID in cspList:
        loop.insert(insert_index, f"_atom_site.chemical_shift_predictor_{cspID}")
        insert_index += 1

    with open(outputFile, 'a') as file:
        if check_for_spaceDelimiter(filename=outputFile):
            print("loop_", file=file)
        elif not check_for_spaceDelimiter(filename=outputFile, delimiter="#") \
                and not check_for_spaceDelimiter(filename=outputFile, delimiter="loop_"):
            print("#", file=file)
            print("loop_", file=file)

        for loop_entry in loop:
            print(loop_entry, file=file)
    return None


def print_aug_atom_site(csDict, augmented_csFilename, af_id, af_entry_name, af_file):
    cf = cif.ReadCif(af_file)
    cifDict = cf.dictionary['-'.join(af_entry_name.split('-')[:3]).lower()]
    afH_atoms = query_afH_atoms(af_id=af_id)
    number_residues = count_residues(af_id)
    atomCount = len(cifDict.block['_atom_site.id'][0])

    print_protonation_loop(outputFile=augmented_csFilename)
    print_csp_loop(outputFile=augmented_csFilename, cspList=list(csDict.keys()))
    print_atom_site_loop(outputFile=augmented_csFilename, cspList=list(csDict.keys()), cifDict=cifDict)

    with open(augmented_csFilename, "a") as cifFilename:
        baseOffset_xref_db_num = int(cifDict.block['_atom_site.pdbx_sifts_xref_db_num'][0][0])
        for atom in afH_atoms:
            xCoord = [float(i) for i in cifDict.block['_atom_site.cartn_x'][0]]
            yCoord = [float(i) for i in cifDict.block['_atom_site.cartn_y'][0]]
            zCoord = [float(i) for i in cifDict.block['_atom_site.cartn_z'][0]]
            xCoordLen = len(str(min(xCoord)))
            yCoordLen = len(str(min(yCoord)))
            zCoordLen = len(str(min(zCoord)))
            coordStrLen = max([xCoordLen, yCoordLen, zCoordLen])

            bFactorList = [float(i) for i in cifDict.block['_atom_site.b_iso_or_equiv'][0]]
            bFacStrLen = len(str(max(bFactorList)))
            xref_db_name_len = len(af_entry_name.split('-')[1])
            xref_db_num_len = len(str(max([int(x) for x in cifDict.block['_atom_site.pdbx_sifts_xref_db_num'][0]])))

            indices = [i for i, x in enumerate(cifDict.block['_atom_site.pdbx_sifts_xref_db_num'][0])
                       if str(int(x) - (baseOffset_xref_db_num - 1)) == str(atom['residue_sequence'])]
            atom_list = []

            for i in indices:
                atom_list.append(cifDict.block['_atom_site.auth_atom_id'][0][i])

            # csValList = defaultdict(int)
            csValList = []
            for cspID in cspID_list:
                # To handle chemical shift predictors that could not predict the chemical shifts of a particular protein
                if cspID not in list(csDict.keys()):
                    csValList.append(".")
                    continue

                csVal = searchDictDictList(dct=csDict, resNum=atom['residue_sequence'], atom=atom['protein_atom'],
                                           cspID=cspID)
                if csVal:
                    # csValList[cspID] = csVal
                    csValList.append(f"{csVal:.3f}")
                else:
                    csValList.append(".")
            if atom['protein_atom'] in atom_list:
                idx = indices[atom_list.index(atom['protein_atom'])]
                print(f"{cifDict.block['_atom_site.group_pdb'][0][int(idx)]: <5}"
                      f"{cifDict.block['_atom_site.id'][0][int(idx)]: <{len(str(atomCount)) + 1}}"
                      f"{cifDict.block['_atom_site.type_symbol'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.label_atom_id'][0][int(idx)]: <4}"
                      f"{cifDict.block['_atom_site.label_alt_id'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.label_comp_id'][0][int(idx)]: <4}"
                      f"{cifDict.block['_atom_site.label_asym_id'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.label_entity_id'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.label_seq_id'][0][int(idx)]: <{len(str(number_residues)) + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_pdb_ins_code'][0][int(idx)]: <2}"
                      f"{float(cifDict.block['_atom_site.cartn_x'][0][int(idx)]): <{coordStrLen + 1}.3f}"
                      f"{float(cifDict.block['_atom_site.cartn_y'][0][int(idx)]): <{coordStrLen + 1}.3f}"
                      f"{float(cifDict.block['_atom_site.cartn_z'][0][int(idx)]): <{coordStrLen + 1}.3f}"
                      f"{float(atom['x_coord']): <{coordStrLen + 1}.3f}"
                      f"{float(atom['y_coord']): <{coordStrLen + 1}.3f}"
                      f"{float(atom['z_coord']): <{coordStrLen + 1}.3f}"
                      f"{' '.join([f'{i:<7}' for i in csValList])} "
                      # f"{' '.join([f'{i:<7.3f}' for i in csValList])}"
                      f"{cifDict.block['_atom_site.occupancy'][0][int(idx)]: <4}"
                      f"{cifDict.block['_atom_site.b_iso_or_equiv'][0][int(idx)]: <{bFacStrLen + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_formal_charge'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.auth_seq_id'][0][int(idx)]: <{len(str(number_residues)) + 1}}"
                      f"{cifDict.block['_atom_site.auth_comp_id'][0][int(idx)]: <4}"
                      f"{cifDict.block['_atom_site.auth_asym_id'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.auth_atom_id'][0][int(idx)]: <4}"
                      f"{cifDict.block['_atom_site.pdbx_pdb_model_num'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_acc'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_name'][0][int(idx)]: <{xref_db_name_len + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_num'][0][int(idx)]: <{xref_db_num_len + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_res'][0][int(idx)]: <1}", file=cifFilename)
            else:
                print(f"{cifDict.block['_atom_site.group_pdb'][0][int(indices[0])]: <5}"
                      f"{atomCount + 1: <{len(str(atomCount)) + 1}}"
                      f"{atom['element']: <2}"
                      f"{atom['protein_atom']: <4}"
                      f"{cifDict.block['_atom_site.label_alt_id'][0][int(indices[0])]: <2}"
                      f"{atom['residue_type']: <4}"
                      f"{cifDict.block['_atom_site.label_asym_id'][0][int(indices[0])]: <2}"
                      f"{cifDict.block['_atom_site.label_entity_id'][0][int(indices[0])]: <2}"
                      f"{atom['residue_sequence']: <{len(str(number_residues)) + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_pdb_ins_code'][0][int(indices[0])]: <2}"
                      f"{'?': <{coordStrLen + 1}}"
                      f"{'?': <{coordStrLen + 1}}"
                      f"{'?': <{coordStrLen + 1}}"
                      f"{float(atom['x_coord']): <{coordStrLen + 1}.3f}"
                      f"{float(atom['y_coord']): <{coordStrLen + 1}.3f}"
                      f"{float(atom['z_coord']): <{coordStrLen + 1}.3f}"
                      f"{' '.join([f'{i:<7}' for i in csValList])} "
                      f"{cifDict.block['_atom_site.occupancy'][0][int(indices[0])]: <4}"
                      f"{cifDict.block['_atom_site.b_iso_or_equiv'][0][int(indices[0])]: <{bFacStrLen + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_formal_charge'][0][int(indices[0])]: <2}"
                      f"{'?': <{len(str(number_residues)) + 1}}"
                      f"{'?': <4}"
                      f"{'?': <2}"
                      f"{'?': <4}"
                      f"{cifDict.block['_atom_site.pdbx_pdb_model_num'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_acc'][0][int(idx)]: <2}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_name'][0][int(idx)]: <{xref_db_name_len + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_num'][0][int(idx)]: <{xref_db_num_len + 1}}"
                      f"{cifDict.block['_atom_site.pdbx_sifts_xref_db_res'][0][int(idx)]: <1}", file=cifFilename)
                atomCount += 1


def print_software(csDict, augmented_csFilename):
    with open(augmented_csFilename) as file:
        lines = file.readlines()

    line_number_start = find_line_number(lines=lines, string_to_parse='_software.classification')
    line_number_stop = find_line_number(lines=lines[line_number_start:], string_to_parse='#') + line_number_start

    keyList = []
    for line in lines[line_number_start:line_number_stop]:
        if '_software.' in line:
            keyList.append(line.rstrip())

    myDict = {key: list() for key in keyList}
    for line in lines[line_number_start:line_number_stop]:
        if '_software.' in line:
            continue
        else:
            cleanedLine = line.rstrip()
            spline = shlex.split(cleanedLine)
            for idx, key in enumerate(keyList):
                myDict[key].append(spline[idx])


    odict = defaultdict(lambda: OrderedDict())
    odict[1]['_software.name'] = 'Sparta+'
    odict[2]['_software.name'] = 'SHIFTX2'
    odict[3]['_software.name'] = 'LarmorCA'
    odict[4]['_software.name'] = 'RCS'
    odict[5]['_software.name'] = 'SHIFTS'
    odict[6]['_software.name'] = 'CheShift'
    odict[8]['_software.name'] = 'UCBSHIFT'
    odict[1]['_software.version'] = '\"2.70F1 Rev 2012.029.12.03\"'
    odict[2]['_software.version'] = '\"Ver 1.10A\"'
    odict[3]['_software.version'] = 'v1.00'
    odict[4]['_software.version'] = '?'
    # TODO: determine RCS vesion and UCBSHIFT
    odict[5]['_software.version'] = '\"Version 5.6\"'
    odict[6]['_software.version'] = 'v3.6'
    odict[8]['_software.version'] = '?'

    cspList = list(csDict.keys())
    for cspID in cspList:
        if cspID in list(odict.keys()):
            for key in myDict.keys():
                if list(set(myDict[key])).__len__() == 1:
                    myDict[key].append(list(set(myDict[key]))[0])
                elif key == '_software.type':
                    myDict[key].append("package")
                elif key == '_software.pdbx_ordinal':
                    myDict[key].append(str(int(myDict[key][-1]) + 1))
                elif key == '_software.description':
                    myDict[key].append(f"\"Chemical shift prediction\"")
                else:
                    myDict[key].append(odict[cspID][key])
    os.remove(augmented_csFilename)

    orderedDict = {idx: OrderedDict() for idx in range(0, myDict['_software.type'].__len__())}
    for dictIdx in list(orderedDict.keys()):
        orderedDict[dictIdx] = {key: None for key in keyList}
    for idx in range(0, myDict['_software.type'].__len__()):
        for key in keyList:
            orderedDict[idx][key] = myDict[key][idx]

    # print to file original two ends of the file with updated sliced data
    with open(augmented_csFilename, mode='a', encoding='utf-8') as myfile:
        myfile.write(''.join(lines[:line_number_start-2]))
    print_loop_multiVal(filename=augmented_csFilename, orderedDict=orderedDict)
    with open(augmented_csFilename, mode='a', encoding='utf-8') as myfile:
        myfile.write(''.join(lines[line_number_stop:]))


def print_authorList(augmented_csFilename):
    with open(augmented_csFilename) as file:
        lines = file.readlines()

    line_number_start = find_line_number(lines=lines, string_to_parse='_audit_author.pdbx_ordinal')
    line_number_stop = find_line_number(lines=lines[line_number_start:], string_to_parse='#') + line_number_start

    lines_authorNames = lines[line_number_start+1:line_number_stop]

    os.remove(augmented_csFilename)
    with open(augmented_csFilename, mode='a', encoding='utf-8') as myfile:
        myfile.write(''.join(lines[:line_number_start]))
        print(f"_audit_author.ORCID", file=myfile)
        print(f"_audit_author.address", file=myfile)
        myfile.write(''.join(lines_authorNames))
        print(
            f"\"Craft, D. Levi\"             "
            f"34"
            f" 0000-0003-3077-3402 Department of Molecular Biology and Biophysics University "
            f"of Connecticut Health Center 263 Farmington Ave, Farmington CT 06030\"", file=myfile)
        print(
            f"\"Schuyler, Adam D.\"          35 0000-0001-7583-899X Department of Molecular Biology and Biophysics University "
            f"of Connecticut Health Center 263 Farmington Ave, Farmington CT 06030\"", file=myfile)
        print(
            f"\"Gryk, Michael R.\"           36 0000-0002-3483-8384 Department of Molecular Biology and Biophysics University "
            f"of Connecticut Health Center 263 Farmington Ave, Farmington CT 06030\"", file=myfile)
        myfile.write(''.join(lines[line_number_stop:]))


def main():
    parser = argparse.ArgumentParser(description='You can add a description here')
    parser.add_argument('--cfg_file', help='cfg filename', required=True)
    parser.add_argument('--afPath', help='path to either an AlphaFold file or directory of AlphaFold files',
                        required=True)
    parser.add_argument('--outputPath', help='destination of augmented mmCIF files', required=True)
    parser.add_argument('--mappingFile', help='flat file of AF to BMRB mappings', required=True)

    args = parser.parse_args()
    global cfgFile
    cfgFile = args.cfg_file

    global afList
    td = timedomain(cfgFile=cfgFile)
    afList = td.query(
        basename='selectAll_afID').data

    global cspID_list
    cspID_list = [1, 2, 3, 4, 5, 6, 8]

    global mapping_file
    mapping_file = args.mappingFile

    # For debugging purposes lets work with a single file
    # inputPath = '/reboxitory/2021/07/alphafold/UP000002485/AF-O94312-F1-model_v1.cif'
    # inputPath = '/reboxitory/2021/07/alphafold/UP000005640/AF-Q4G0P3-F15-model_v1.cif'
    # inputPath = '/reboxitory/2021/07/alphafold/UP000005640/AF-Q9BYW2-F1-model_v1.cif'
    # augment_mmCIF(inputPath=inputPath, outputPath=args.outputPath)

    augment_mmCIF(inputPath=args.afPath, outputPath=args.outputPath)


if __name__ == '__main__':
    main()
