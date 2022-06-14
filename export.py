import copy
import databroker
import datetime
import h5py
import json
import os
import prefect
import threading
import numpy as np
import epics
import shutil
import socket
import warnings

from collections import deque, Mapping
from lixtools.atsas import gen_report
from lixtools.hdf import h5sol_HPLC,h5sol_HT
from prefect import task, Flow, Parameter
from py4xs.detector_config import create_det_from_attrs
from py4xs.hdf import h5xs,h5exp

packing_queue_sock_port = 9999

@task
def run_export_lix(uids):

    """
    This function access the data via tiled and read relevant metadata in order
    to start workflows via Prefect.
    """

    tiled_client = databroker.from_profile("nsls2", username=None)["lix"]
    runs = [tiled_client[uid] for uid in uids]
    task_info = {run.start['uid']:run.start['plan_name'] for run in runs}

    logger = prefect.context.get("logger")
    logger.info(f"Processing: {task_info}")

    # TODO: Need to fix the line below
    pack_and_process(runs, filepath="/nsls2/data/data/dssi/scratch/prefect-outputs/lix")


with Flow("export") as flow:
    uids = Parameter("uids")
    run_export_lix(uids)


def h5_fix_sample_name(filename_h5):
    """ the hdf5 file is assumed to have top-level groups that each corresponds to a sample
    """
    f = h5py.File(filename_h5, "r+")
    grps = list(f.keys())
    for g in grps:
        header = json.loads(f[g].attrs.get('start'))
        if 'sample_name' in header.keys():
            sn = header['sample_name']
            f.move(g, sn)
    f.close()


def compile_replace_res_path(run):

    """ protocol prior to May 2022:
            md['data_path'] specifies the directories all data files are supposed to go
                e.g. /nsls2/xf16id1/data/2022-1/310121/308824
            the original location of the data is recorded in the databroker, but not in the meta data
            however, this location should follow the format of the {pilatus_data_dir}/{proposal_id}/{run_id}
        protocol since May 2022:
            md['data_path'] specifies where all IOC data are supposed to go
                e.g. /nsls2/data/lix/legacy/%s/2022-1/310032/test
            md['pilatus']['ramdisk'] specifies where the Pilatus data are originally saved
                e.g. /exp_path/hdf
    """
    md = run.start
    ret = {}
    dpath = md['data_path']
    try:
        ret[md['pilatus']['ramdisk']] = dpath.split("%s")[0]
    except:
        cycle_id = re.search("20[0-9][0-9]-[0-9]", dpath)[0]
        ret[pilatus_data_dir] = dpath.split(cycle_id)[0]+cycle_id

    return ret


def pack_h5(runs, filepath='', filename=None, fix_sample_name=True, stream_name=None,
            attach_uv_file=False, delete_old_file=True, include_motor_pos=True, debug=False,
            fields=['em2_current1_mean_value', 'em2_current2_mean_value',
                    'em1_sum_all_mean_value', 'em2_sum_all_mean_value', 'em2_ts_SumAll', 'em1_ts_SumAll',
                    'xsp3_spectrum_array_data', "pilatus_trigger_time",
                    'pil1M_image', 'pilW1_image', 'pilW2_image',
                    'pil1M_ext_image', 'pilW1_ext_image', 'pilW2_ext_image'], replace_res_path={}):

    """ if only 1 uid is given, use the sample name as the file name
        any metadata associated with each uid will be retained (e.g. sample vs buffer)

        to avoid multiple processed requesting packaging, only 1 process is allowed at a given time
        this is i
    """

    if len(runs) > 1:
        if filename is None:
            raise Exception("a file name must be given for a list of uids.")
        plan_names = [run.start['plan_name'] for run in runs]
        if len(set(plan_names)) > 1:
            raise Exception("mixed plan names in uids: %s" % plan_names)
    else:
        header = db[uids]
        if filename is None:
            if "sample_name" in list(header.start.keys()):
                filename = header.start['sample_name']
            else:
                fds = header.fields()
                # find the first occurance of _file_file_name in fields
                f = next((x for x in fds if "_file_file_name" in x), None)
                if f is None:
                    raise Exception("could not automatically select a file name.")
                filename = header.table(fields=[f])[f][1]

    # if replace_res_path is not specified, try to figure out whether it is necessary
    if len(replace_res_path)==0:
        replace_res_path = compile_replace_res_path(runs[0])

    fds0 = headers[0].fields()
    # only these fields are considered relevant to be saved in the hdf5 file
    fds = list(set(fds0) & set(fields))
    if 'motors' in list(runs[0].start.keys()) and include_motor_pos:
        for m in runs[0].start['motors']:
            fds += [m] #, m+"_user_setpoint"]

    if filename[-3:]!='.h5':
        filename += '.h5'

    if filepath!='':
        if not os.path.exists(filepath):
            raise Exception(f'{filepath} does not exist.')
        filename = os.path.join(filepath, filename)

    if delete_old_file:
        try:
            os.remove(filename)
        except OSError:
            pass

    print(fds)
    hdf5_export(runs, filename, fields=fds, stream_name=stream_name, use_uid=False,
                replace_res_path=replace_res_path, debug=debug) #, mds= db.mds, use_uid=False)

    # by default the groups in the hdf5 file are named after the scan IDs
    if fix_sample_name:
        h5_fix_sample_name(filename)

    if attach_uv_file:
        # by default the UV file should be saved in /nsls2/xf16id1/Windows/
        # ideally this should be specified, as the default file is overwritten quickly
        h5_attach_hplc(filename, '/nsls2/xf16id1/Windows/hplc_export.txt')

    print(f"finished packing {filename} ...")
    return filename


def h5_attach_hplc(filename_h5, filename_hplc, chapter_num=-1, grp_name=None):
    """ the hdf5 is assumed to contain a structure like this:
        LIX_104
        == hplc
        ==== data
        == primary (em, scattering patterns, ...)

        attach the HPLC data to the specified group
        if the group name is not give, attach to the first group in the h5 file
    """
    f = h5py.File(filename_h5, "r+")
    if grp_name == None:
        grp_name = list(f.keys())[0]

    hdstr, dhplc = readShimadzuDatafile(filename_hplc, chapter_num=chapter_num )
    # 3rd line of the header contains the HPLC data file name, which is based on the sample name
    sname = hdstr.split('\n')[2].split('\\')[-1][:-4]
    if grp_name!=sname:
        print(f"mismatched sample name: {sname} vs {grp_name}")
        f.close()
        return

    # this group is created by suitcase if using flyer-based hplc_scan
    # otherwise it has to be created first
    # it is also possible that there was a previous attempt to populate the data
    # but the data source/shape is incorrect -> delete group first
    if 'hplc' in f[f"{grp_name}"].keys():
        grp = f["%s/hplc/data" % grp_name]
    else:
        grp = f.create_group(f"{grp_name}/hplc/data")

    if grp.attrs.get('header') == None:
        grp.attrs.create("header", np.asarray(hdstr, dtype=np.string_))
    else:
        grp.attrs.modify("header", np.asarray(hdstr, dtype=np.string_))

    existing_keys = list(grp.keys())
    for k in dhplc.keys():
        d = np.asarray(dhplc[k]).T
        if k in existing_keys:
            print("warning: %s already exists, deleting ..." % k)
            del grp[k]
        dset = grp.require_dataset(k, d.shape, d.dtype)
        dset[:] = d

    f.close()


def send_to_packing_queue_remote(uid, datatype, froot=data_file_path.gpfs, move_first=False):
    """ data_type must be one of ["scan", "flyscan", "HPLC", "sol", "multi", "mscan"]
        single uid only for "scan", "flyscan", "HPLC"
        uids must be concatenated using '|' for "multi" and "sol"
        if move_first is True, move the files from RAMDISK to GPFS first, otherwise the RAMDISK
            may fill up since only one pack_h5 process is allow
    """
    if datatype not in ["scan", "flyscan", "HPLC", "multi", "sol", "mscan", "mfscan"]:
        raise Exception("invalid data type: {datatype}, valid options are scan and HPLC.")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(('xf16id-srv1', packing_queue_sock_port))
    msg = f"{datatype}::{uid}::{proc_path}::{froot.name}::{move_first}"
    s.send(msg.encode('ascii'))
    s.close()

def pack_and_process(runs, data_type, filepath=""):

    # useful for moving files from RAM disk to GPFS during fly scans
    #
    # assume other type of data are saved on RAM disk as well (GPFS not working for WAXS2)
    # these data must be moved manually to GPFS
    #global pilatus_trigger_mode  #,CBF_replace_data_path

    if data_type not in ["scan", "flyscan", "HPLC", "multi", "sol", "mscan", "mfscan"]:
        raise Exception("invalid data type: {datatype}, valid options are scan and HPLC.")

    if data_type not in ["multi", "sol", "mscan", "mfscan"]: # single UID
        if 'exit_status' not in run.stop.keys():
            print(f"in complete header for {runs[0].start['uid']}.")
            return
        if run.stop['exit_status'] != 'success': # the scan actually finished
            print(f"scan {runs[0].start['uid']} was not successful.")
            return

    t0 = time.time()
    # if the filepath contains exp.h5, read detectors/qgrid from it
    try:
        dt_exp = h5exp(filepath+'/exp.h5')
    except:
        dt_exp = None

    dir_name = None

    if data_type in ["multi", "sol", "mscan", "mfscan"]:

        uids = [run.start['uid'] for run in runs]

        if data_type=="sol":
            sb_dict = json.loads(uids.pop())
        ## assume that the meta data contains the holderName
        if 'holderName' not in list(runs[0].start.keys()):
            print("cannot find holderName from the header, using tmp.h5 as filename ...")
            fh5_name = "tmp.h5"
        else:
            dir_name = runs[0].start['holderName']
            fh5_name = dir_name+'.h5'
        filename = pack_h5(runs, filepath, filename="tmp.h5")
        if filename is not None and dt_exp is not None and data_type!="mscan":
            print('processing ...')
            if data_type=="sol":
                dt = h5sol_HT(filename, [dt_exp.detectors, dt_exp.qgrid])
                dt.assign_buffer(sb_dict)
                dt.process(filter_data=True, sc_factor="auto", debug='quiet')
                #dt.export_d1s(path=filepath+"/processed/")
            elif data_type=="multi":
                dt = h5xs(filename, [dt_exp.detectors, dt_exp.qgrid], transField='em2_sum_all_mean_value')
                dt.load_data(debug="quiet")
            elif data_type=="mfscan":
                dt = h5xs(filename, [dt_exp.detectors, dt_exp.qgrid])
                dt.load_data(debug="quiet")
            dt.fh5.close()
            del dt,dt_exp
            if fh5_name != "tmp.h5":  # temporary fix, for some reason other processes cannot open the packed file
                os.system(f"cd {filepath} ; cp tmp.h5 {fh5_name} ; rm tmp.h5")
            if data_type == "sol":
                try:
                    gen_report(fh5_name)
                except:
                    pass
    elif data_type=="HPLC":
        filename = pack_h5(runs, filepath=filepath, attach_uv_file=True)
        if filename is not None and dt_exp is not None:
            print('procesing ...')
            dt = h5sol_HPLC(filename, [dt_exp.detectors, dt_exp.qgrid])
            dt.process(debug='quiet')
            dt.fh5.close()
            del dt,dt_exp
    elif data_type=="flyscan" or data_type=="scan":
        filename = pack_h5(runs, filepath=filepath)
    else:
        print(f"invalid data type: {data_type} .")
        return

    if filename is None:
        return # packing unsuccessful,
    print(f"{time.asctime()}: finished packing/processing, total time lapsed: {time.time()-t0:.1f} sec ...")


def process_packing_queue():
    """ this should only run on xf16idc-gpu1, moved to srv1 Mar 2022
        needed for HPLC run and microbeam mapping
    """
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    host = socket.gethostname()
    if host!='xf16id-srv1' and host!="xf16id-srv1.nsls2.bnl.local":
        raise Exception(f"this function can only run on xf16id-srv1, not {host}.")
    serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serversocket.bind(('xf16id-srv1', packing_queue_sock_port))
    serversocket.listen(5)
    print('listening ...')

    while True:
        clientsocket,addr = serversocket.accept()
        print(f"{time.asctime()}: got a connection from {addr} ...")
        msg = clientsocket.recv(8192).decode()
        print(msg)
        clientsocket.close()
        data_type,uid,path,frn,t = msg.split("::")
        if t is True:
            move_first = True
        else:
            move_first = False

        if data_type not in ["multi", "sol", "mscan", "mfscan"]: # single UID
            if 'exit_status' not in db[uid].stop.keys():
                print(f"in complete header for {uid}.")
                return
            if db[uid].stop['exit_status'] != 'success': # the scan actually finished
                print(f"scan {uid} was not successful.")
                return

        threading.Thread(target=pack_and_process, args=(data_type,uid,path,)).start()
        print("processing thread started ...")


def conv_to_list(d): 
    if isinstance(d, float) or isinstance(d, int) or isinstance(d, str): 
        return [d] 
    elif isinstance(d, list):
        if not isinstance(d[0], list):
            return d 
    d1 = []
    for i in d:
        d1 += conv_to_list(i) 
    return d1 

def update_res_path(res_path, replace_res_path={}):
    for rp1,rp2 in replace_res_path.items():
        print("updating resource path ...")
        if rp1 in res_path:
            res_path = res_path.replace(rp1, rp2)  
    return res_path

def locate_h5_resource(res, replace_res_path, debug=False):
    """ this is intended to move h5 file created by Pilatus
        these files are originally saved on PPU RAMDISK, but should be moved to the IOC data directory
        this function will look for the file at the original location, and relocate the file first if it is there
        and return the h5 dataset
    """
    fn_orig = res["root"] + res["resource_path"]
    fn = update_res_path(fn_orig, replace_res_path)
    if debug:
        print(f"resource locations: {fn_orig} -> {fn}")
    
    if not(os.path.exists(fn_orig) or os.path.exists(fn)):
        print(f"could not locate the resource at either {fn} or {fn_orig} ...")
        raise Exception
    if os.path.exists(fn_orig) and os.path.exists(fn) and fn_orig!=fn:
        print(f"both {fn} and {fn_orig} exist, resolve the conflict manually first ..." )
        raise Exception
    if not os.path.exists(fn):
        fdir = os.path.dirname(fn)
        if not os.path.exists(fdir):
            makedirs(fdir, mode=0o2775)
        if debug:
            print(f"copying {fn_orig} to {fdir}")
        tfn = fn+"_partial"
        shutil.copy(fn_orig, tfn)
        os.rename(tfn, fn)
        os.remove(fn_orig)
    
    hf5 = h5py.File(fn, "r")
    return hf5, hf5["/entry/data/data"]


def hdf5_export(headers, filename, debug=False,
           stream_name=None, fields=None, bulk_h5_res=True,
           timestamps=True, use_uid=True, db=None, replace_res_path={}):
    """
    Create hdf5 file to preserve the structure of databroker.
    Parameters
    ----------
    headers : a Header or a list of Headers
        objects retruned by the Data Broker
    filename : string
        path to a new or existing HDF5 file
    stream_name : string, optional
        None means save all the data from each descriptor, i.e., user can define stream_name as primary,
        so only data with descriptor.name == primary will be saved.
        The default is None.
    fields : list, optional
        whitelist of names of interest; if None, all are returned;
        This is consistent with name convension in databroker.
        The default is None.
    timestamps : Bool, optional
        save timestamps or not
    use_uid : Bool, optional
        Create group name at hdf file based on uid if this value is set as True.
        Otherwise group name is created based on beamline id and run id.
    db : databroker object, optional
        db should be included in hdr.
    replace_res_path: in case the resource has been moved, specify how the path should be updated
        e.g. replace_res_path = {"exp_path/hdf": "nsls2/xf16id1/data/2022-1"}
        
    Revision 2021 May
        Now that the resource is a h5 file, copy data directly from the file 
        
    """
    if isinstance(headers, Header):
        headers = [headers]

    with h5py.File(filename, "w") as f:
        #f.swmr_mode = True # Unable to start swmr writing (file superblock version - should be at least 3)
        for header in headers:
            try:
                db = header.db
            except AttributeError:
                pass
            if db is None:
                raise RuntimeError('db is not defined in header, so we need to input db explicitly.')
                
            res_docs = {}
            for n,d in header.documents():
                if n=="resource":
                    res_docs[d['uid']] = d
            if debug:
                print("res_docs:\n", res_docs)
                    
            try:
                descriptors = header.descriptors
            except KeyError:
                warnings.warn("Header with uid {header.uid} contains no "
                              "data.".format(header), UserWarning)
                continue
            if use_uid:
                top_group_name = header.start['uid']
            else:
                top_group_name = 'data_' + str(header.start['scan_id'])
            group = f.create_group(top_group_name)
            _safe_attrs_assignment(group, header)
            for i, descriptor in enumerate(descriptors):
                # make sure it's a dictionary and trim any spurious keys
                descriptor = dict(descriptor)
                if stream_name:
                    if descriptor['name'] != stream_name:
                        continue
                descriptor.pop('_name', None)
                if debug:
                    print(f"processing stream {stream_name}")

                if use_uid:
                    desc_group = group.create_group(descriptor['uid'])
                else:
                    desc_group = group.create_group(descriptor['name'])

                data_keys = descriptor['data_keys']

                _safe_attrs_assignment(desc_group, descriptor)

                # fill can be bool or list
                events = list(header.events(stream_name=descriptor['name'], fill=False))

                res_dict = {}
                for k, v in list(events[0]['data'].items()):
                    if not isinstance(v, str):
                        continue
                    if v.split('/')[0] in res_docs.keys():
                        res_dict[k] = []
                        for ev in events:
                            res_uid = ev['data'][k].split("/")[0]
                            if not res_uid in res_dict[k]:
                                res_dict[k].append(res_uid)

                if debug:
                    print("res_dict:\n", res_dict)

                event_times = [e['time'] for e in events]
                desc_group.create_dataset('time', data=event_times,
                                          compression='gzip', fletcher32=True)
                data_group = desc_group.create_group('data')
                if timestamps:
                    ts_group = desc_group.create_group('timestamps')

                for key, value in data_keys.items():
                    print(f"processing {key} ...")
                    if fields is not None:
                        if key not in fields:
                            print("   skipping ...")
                            continue
                    print(f"creating dataset for {key} ...")
                    if timestamps:
                        timestamps = [e['timestamps'][key] for e in events]
                        ts_group.create_dataset(key, data=timestamps,
                                                compression='gzip',
                                                fletcher32=True)

                    if key in list(res_dict.keys()):
                        res = res_docs[res_dict[key][0]]
                        print(f"processing resource ...\n", res)

                        # pilatus data, change the path from ramdisk to IOC data directory
                        if key in ["pil1M_image", "pilW2_image"]:
                            rp = {pilatus_data_dir: data_destination}

                        if res['spec'] == "AD_HDF5" and bulk_h5_res:
                            rawdata = None
                            N = len(res_dict[key])
                            print(f"copying data from source h5 file(s) directly, N={N} ...")
                            if N==1:
                                hf5, data = locate_h5_resource(res_docs[res_dict[key][0]], replace_res_path=rp, debug=debug)
                                data_group.copy(data, key)
                                hf5.close()
                                dataset = data_group[key]
                            else: # ideally this should never happen, only 1 hdf5 file/resource per scan
                                for i in range(N):
                                    hf5, data = locate_h5_resource(res_docs[res_dict[key][i]])
                                    if i==0:
                                        dataset = data_group.create_dataset(
                                                key, shape=(N, *data.shape), 
                                                compression=data.compression,
                                                chunks=(1, *data.chunks))
                                    dataset[i,:] = data
                                    hf5.close()
                        else:
                            print(f"getting resource data using handlers ...")
                            rawdata = header.table(stream_name=descriptor['name'], 
                                                   fields=[key], fill=True)[key]   # this returns the time stamps as well
                    else:
                        print(f"compiling resource data from individual events ...")
                        rawdata = [e['data'][key] for e in events]

                    if rawdata is not None:
                        data = np.array(rawdata)

                        if value['dtype'].lower() == 'string':  # 1D of string
                            data_len = len(data[0])
                            data = data.astype('|S'+str(data_len))
                            dataset = data_group.create_dataset(
                                key, data=data, compression='gzip')
                        elif data.dtype.kind in ['S', 'U']:
                            # 2D of string, we can't tell from dytpe, they are shown as array only.
                            if data.ndim == 2:
                                data_len = 1
                                for v in data[0]:
                                    data_len = max(data_len, len(v))
                                data = data.astype('|S'+str(data_len))
                                dataset = data_group.create_dataset(
                                    key, data=data, compression='gzip')
                            else:
                                raise ValueError(f'Array of str with ndim >= 3 can not be saved: {key}')
                        else:  # save numerical data
                            try:
                                if isinstance(rawdata, list):
                                    blk = rawdata[0]
                                else:
                                    blk = rawdata[1]
                                if isinstance(blk, np.ndarray): # detector image
                                    data = np.vstack(rawdata)
                                    chunks = np.ones(len(data.shape), dtype=int)
                                    n = len(blk.shape)
                                    if chunks[-1]<10:
                                        chunks[-3:] = data.shape[-3:]
                                    else:
                                        chunks[-2:] = data.shape[-2:]
                                    chunks = tuple(chunks)
                                    print("data shape: ", data.shape, "     chunks: ", chunks)
                                    dataset = data_group.create_dataset(
                                        key, data=data,
                                        compression='gzip', fletcher32=True, chunks=chunks)
                                else: # motor positions etc.
                                    data = np.array(conv_to_list(rawdata)) # issue with list of lists
                                    chunks = False
                                    dataset = data_group.create_dataset(
                                        key, data=data,
                                        compression='gzip', fletcher32=True)
                            except:
                                raise
                            #    print("failed to convert data: ")
                            #    print(np.array(conv_to_list(rawdata)))
                            #    continue

                    # Put contents of this data key (source, etc.)
                    # into an attribute on the associated data set.
                    _safe_attrs_assignment(dataset, dict(value))


def _clean_dict(d):
    d = dict(d)
    for k, v in list(d.items()):
        # Store dictionaries as JSON strings.
        if isinstance(v, Mapping):
            d[k] = _clean_dict(d[k])
            continue
        try:
            json.dumps(v)
        except TypeError:
            d[k] = str(v)
    return d


def _safe_attrs_assignment(node, d):
    d = _clean_dict(d)
    for key, value in d.items():
        # Special-case None, which fails too late to catch below.
        if value is None:
            value = 'None'
        # Try storing natively.
        try:
            node.attrs[key] = value
        # Fallback: Save the repr, which in many cases can be used to
        # recreate the object.
        except TypeError:
            node.attrs[key] = json.dumps(value)

def filter_fields(headers, unwanted_fields):
    """
    Filter out unwanted fields.
    Parameters
    ----------
    headers : doct.Document or a list of that
        returned by databroker object
    unwanted_fields : list
        list of str representing unwanted filed names
    Returns
    -------
    set:
        set of selected names
    """
    if isinstance(headers, Header):
        headers = [headers]
    whitelist = set()
    for header in headers:
        for descriptor in header.descriptors:
            good = [key for key in descriptor.data_keys.keys()
                    if key not in unwanted_fields]
            whitelist.update(good)
    return whitelist
