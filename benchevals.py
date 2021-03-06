#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:Description: Evaluation of results produced by each executed application.

	Resulting cluster/community structure is evaluated using extrinsic (NMI, NMI_s)
	and intrinsic (Q - modularity) measures considering overlaps.


	def execQmeasure(execpool, qualsaver, smeta, qparams, cfpath, inpfpath, asym=False
	, timeout=0, seed=None, task=None, workdir=UTILDIR, revalue=True):
		Quality measure executor (stub)

		xmeasures  - various extrinsic quality measures

		execpool: ExecPool  - execution pool
		qualsaver: QualitySaver  - quality results saver (persister)
		smeta: SMeta - serialization meta data
		qparams: iterable(str)  - quality measures parameters (arguments excluding the clustering and network files)
		cfpath: str  - file path of the clustering to be evaluated
		inpfpath: str  - input dataset file path (ground-truth / input network for the ex/in-trinsic quality measure)
		asym: bool  - whether the input network is asymmetric (directed, specified by arcs)
		timeout: uint  - execution timeout in seconds, 0 means infinity
		seed: uint  - seed for the stochastic qmeasures
		task: Task  - owner (super) task
		workdir: str  - working directory of the quality measure (qmeasure location)
		revalue: bool  - whether to revalue the existent results or omit such evaluations
			calculating and saving only the values which are not present in the dataset.
			NOTE: The default value is True because of the straight forward out of the box implementation.
			ATTENTION: Not all quality measure implementations might support early omission
				of the calculations on revalue=False, in which case a warning should be issued.

		return  jobsnum: uint  - the number of started jobs


:Authors: (c) Artem Lutov <artem@exascale.info>
:Organizations: eXascale Infolab <http://exascale.info/>, Lumais <http://www.lumais.com/>, ScienceWise <http://sciencewise.info/>
:Date: 2015-12
"""

from __future__ import print_function, division  # Required for stderr output, must be the first import
import os
# import shutil
import glob
import sys
import traceback  # Stack trace
import time
# Consider time interface compatibility for Python before v3.3
if not hasattr(time, 'perf_counter'):  #pylint: disable=C0413
	time.perf_counter = time.time

from subprocess import PIPE
# Queue is required to asynchronously save evaluated quality measures to the persistent storage
from multiprocessing import cpu_count  # , Value, sharedctypes, Process, Queue
# try:
# 	import queue  # queue in Python3
# except ImportError:  # Queue in Python2
# 	import Queue as queue  # For exceptions handling: queue.Full, etc.

# Required for the aggregation of the quality evaluations
import math
# import copy
import fnmatch  # Matching of the name wildcards
import itertools  # chain
from numbers import Number  # To verify that a variable is a number (int or float)
from collections import namedtuple
# Required for the quality evaluation persistence
import numpy as np  # Required for the HDF5 operations
import h5py  # HDF5 storage

# from benchapps import  # funcToAppName,
from benchutils import viewitems, viewvalues, syncedTime, \
 tobackup, funcToAppName, staticTrace, \
 SEPPARS, UTILDIR, ALGSDIR, \
 TIMESTAMP_START  #, escapePathWildcards, envVarDefined, SEPPATHID, SEPINST, TIMESTAMP_START_HEADER, TIMESTAMP_START_STR
from utils.mpepool import Task, Job, AffinityMask

# Identify type of the Variable-length ASCII (bytes) / UTF8 types for the HDF5 storage
try:
	# For Python3
	h5str = h5py.special_dtype(vlen=bytes)  # ASCII str, bytes
	h5ustr = h5py.special_dtype(vlen=str)  # UTF8 str
	# Note: str.encode() converts str to bytes, str.decode() converts bytes to (Unicode) str
except NameError:  # bytes are not defined in Python2
	# For Python2
	h5str = h5py.special_dtype(vlen=str)  # ASCII str, bytes
	h5ustr = h5py.special_dtype(vlen=unicode)  #pylint: disable=E0602;  # UTF8 str
	# Note: str.decode() converts bytes to Unicode str, str.encode() converts (Unicode) str to bytes

# Note: '/' is required in the end of the dir to evaluate whether it is already exist and distinguish it from the file
RESDIR = 'results/'  # Final accumulative results of .mod, .nmi and .rcp for each algorithm, specified RELATIVE to ALGSDIR
CLSDIR = 'clusters/'  # Clusters directory for the resulting clusters of algorithms execution
QMSDIR = 'qmeasures/'  # Quality measures standard output and logs directory (QMSDIR/<basenet>/*.{log,err})
# _QUALITY_STORAGE = 'quality.h5'  # Quality evaluation storage file name
EXTLOG = '.log'  # Extension for the logs (stdout redirection and notifications)
EXTERR = '.err'  # Extension for the errors (stderr redirection and errors tracing)
#_EXTERR = '.elog'  # Extension for the unbuffered (typically error) logs
EXTRESCONS = '.rcp'  # Extension for the Resource Consumption Profile (made by the exectime app)
EXTAGGRES = '.res'  # Extension for the aggregated results
EXTAGGRESEXT = '.resx'  # Extension for the extended aggregated results
_EXTQDATASET = '.dat'  # Extension for the HDF5 datasets
# Job/Task name parts separator ('/' is the best choice because it can not appear in a file name,
# which can be part of job name)
SEPNAMEPART = '/'
_SEPQARGS = '_'  # Separator for the quality measure arguments to be shown in the monitoring and results
# Separetor for the quality measure from the processing file name.
# It is used for the file names and should follow restrictions on the allowed symbols.
_SEPQMS = ';'
_SUFULEV = '+u'  # Unified levels suffix of the HDF5 dataset (actual for DAOC)
_PREFMETR = ':'  # Metric prefix in the HDF5 dataset name
SATTRNINS = 'nins'  # HDF5 storage object attribute for the number of network instances
SATTRNSHF = 'nshf'  # HDF5 storage object attribute for the number of network instance shuffles
SATTRNLEV = 'nlev'  # HDF5 storage object attribute for the number of clustering levels

QMSRAFN = {}  # Specific affinity mask of the quality measures: str, AffinityMask;  qmsrAffinity
QMSINTRIN = set()  # Intrinsic quality measures requiring input network instead of the ground-truth clustering
QMSRUNS = {}  # Run the respective stochastic quality measures specified number of times
# Note: the metrics producing by the measure can be defined by the execution arguments
# QMSMTS = {}  # Metrics of the respective quality measures, omission means availability of the single metric with same name as the measuring executor

_DEBUG_TRACE = False  # Trace start / stop and other events to stderr

# # Accessory Routines -----------------------------------------------------------
# def toH5str(text):
# 	"""Convert text to the h5str
#
# 	text: str  - the text to be converted
#
# 	return  h5str  - the converted text
# 	"""
# 	return text.encode()  # Required for Python3, stub in Python2
#
#
# def toH5ustr(text):
# 	"""Convert text to the h5ustr
#
# 	text: str  - the text to be converted
#
# 	return  h5ustr  - the converted text
# 	"""
# 	return text.decode()  # Required for Python2, stub in Python3

# class Measures(object):
# 	"""Quality Measures"""
# 	def __init__(self, eval_num=None, nmi_max=None, nmi_sqrt=None, onmi_max=None, onmi_sqrt=None
# 	, f1p=None, f1h=None, f1s=None, mod=None, cdt=None):
# 		"""Quality Measures to be saved
#
# 		eval_num  - number/id of the evaluation to take average over multiple (re)evaluations
# 			(NMI from gecmi provides stochastic results), uint8 or None
# 		nmi_max  - NMI multi-resolution overlapping (gecmi) normalized by max (default)
# 		nmi_sqrt  - NMI multi-resolution overlapping (gecmi) normalized by sqrt
# 		onmi_max  - Overlapping nonstandard NMI (onmi) normalized by max (default)
# 		onmi_sqrt  - Overlapping nonstandard NMI (onmi) normalized by sqrt
# 		f1p  - F1p measure (harmonic mean of partial probabilities)
# 		f1h  - harmonic F1-score measure (harmonic mean of weighted average F1 measures)
# 		f1s  - average F1-score measure (arithmetic mean of weighted average F1 measures)
# 		mod  - modularity
# 		cdt  - conductance
# 		"""
# 		assert ((eval_num is None or (isinstance(eval_num, int) and 0 <= eval_num <= 0xFF)) and
# 			(nmi_max is None or 0. <= nmi_max <= 1.) and (nmi_sqrt is None or 0. <= nmi_sqrt <= 1.) and
# 			(onmi_max is None or 0. <= onmi_max <= 1.) and(onmi_sqrt is None or 0. <= onmi_sqrt <= 1.) and
# 			(f1p is None or 0. <= f1p <= 1.) and (f1h is None or 0. <= f1h <= 1.) and (f1s is None or 0. <= f1s <= 1.) and
# 			(mod is None or -0.5 <= mod <= 1.) and (cdt is None or 0. <= cdt <= 1.)), (
# 			'Parameters validation failed  nmi_max: {}, nmi_sqrt: {}, eval_num: {}, onmi_max: {}, onmi_sqrt: {}'
# 			', f1p: {}, f1h: {}, f1s: {}, q: {}, cdt: {}'.format(nmi_max, nmi_sqrt, eval_num
# 			, onmi_max, onmi_sqrt, f1p, f1h, f1s, mod, cdt))
# 		self._eval_num = eval_num  # Note: distinct attr name prefix ('_') is used to distinguish from the measure name
# 		self.nmi_max = nmi_max
# 		self.nmi_sqrt = nmi_sqrt
# 		self.onmi_max = onmi_max
# 		self.onmi_sqrt = onmi_sqrt
# 		self.f1p = f1p
# 		self.f1h = f1h
# 		self.f1s = f1s
# 		self.mod = mod  # Modularity
# 		self.cdt = cdt  # Conductance
#
#
# 	def __str__(self):
# 		"""String conversion"""
# 		return ', '.join([': '.join((name, str(val))) for name, val in viewitems(self.__dict__)])

# class NetParams(object):
# 	__slots__ = ('asym', 'pathidsuf')

# 	def __init__(self, asym, pathidsuf=''):
# 		"""Parameters of the input network

# 		asym: bool  - the input network might be asymmetric (directed) and is specified by arcs rather than edges
# 		pathidsuf: str  - network path id prepended with the path separator, used to distinguish nets
# 			with the same name located in different dirs
# 		"""
# 		assert not pathidsuf or pathidsuf.startswith(SEPPATHID), 'Ivalid pathidsuf: ' + pathidsuf
# 		self.asym = asym
# 		self.pathidsuf = pathidsuf

# 	def __str__(self):
# 		"""String conversion"""
# 		return ', '.join([': '.join((name, str(self.__getattribute__(name)))) for name in self.__slots__])


class NetInfo(object):
	"""Network Metainformation"""
	__slots__ = ('nins', 'nshf')  # , 'gvld'

	def __init__(self, nins=1, nshf=1):
		"""Network metainformation

		nins: uint8 >= 1  - the number of instances including the origin
		nshf: uint8 >= 1  - the number of shuffles including the origin
		"""
		# gvld: bool or None  - whether the respective HDF5 group entry attributes have been validated for this netinfo
		# 	(exception is raised on the failed validation)
		assert nins >= 1 and isinstance(nins, int) and nshf >= 1 and isinstance(
			nshf, int), 'Invalid arguments  nins: {}, nshf: {}'.format(nins, nshf)
		self.nins = nins
		self.nshf = nshf
		# self.gvld = False

	def __str__(self):
		"""String conversion"""
		return ', '.join([': '.join((name, str(self.__getattribute__(name))))for name in self.__slots__])


class SMeta(object):
	"""Serialization meta information (data cell location)"""
	__slots__ = ('group', 'measure', 'ulev', 'iins', 'ishf', 'ilev', 'irun')

	def __init__(self, group, measure, ulev, iins, ishf, ilev=0, irun=0):
		"""Serialization meta information (location in the storage)

		group: str  - h5py.Group name where the target dataset is located: <algname>/<basenet><pathid>/
		measure: str  - name of the serializing evaluation measure
		ulev: bool  - unified levels, the clustering consists of the SINGLE (unified) level containing
			(representative) clusters from ALL (multiple) resolutions
		netinf: NetInfo  - network meta information (the number of network instances and shuffles, etc.)
		pathidsuf: str  - network path id prepended with the path separator
		ilev: uint  - index of the clustering level
		irun: uint8  - run id (iteration)
		"""
		# alg: str  - algorithm name, required to only to structure (order) the output results

		assert isinstance(group, str) and isinstance(measure, str) and iins >= 0 and isinstance(
			iins, int) and ishf >= 0 and isinstance(ishf, int) and ilev >= 0 and isinstance(
			ilev, int) and irun >= 0 and isinstance(irun, int), (
			'Invalid arguments:\n\tgroup: {group}\n\tmeasure: {measure}\n\tulev: {ulev}\n\t'
			'iins: {iins}\n\tishuf: {ishf}\n\tilev: {ilev}\n\tirun: {irun}'.format(
			group=group, measure=measure, ulev=ulev, iins=iins, ishf=ishf, ilev=ilev, irun=irun))
		self.group = group  # Group name since the opened group object can not be marshaled to another process
		self.measure = measure
		self.ulev = ulev
		self.iins = iins
		self.ishf = ishf
		self.ilev = ilev
		self.irun = irun

	def __str__(self):
		"""String conversion"""
		return ', '.join([': '.join((name, str(self.__getattribute__(name)))) for name in self.__slots__])


class QEntry(object):
	"""Quality evaluations etry to be saved to the persistent storage"""
	__slots__ = ('smeta', 'data')

	def __init__(self, smeta,
		data):  #, appargs=None, level=0, instance=0, shuffle=0):
		"""Quality evaluations to be saved

		smeta: SMeta  - serialization meta data
		data: dict(name: str, val: float32)  - serializing data
		"""
		assert isinstance(smeta, SMeta) and data and isinstance(data, dict), (
			'Invalid type of the arguments, smeta: {}, data: {}'.format(
			type(smeta).__name__, type(data).__name__))
		# # Validate first item in the data
		# name, val = next(iter(data))
		# assert isinstance(name, str) and isinstance(val, float), (
		# 	'Invalid type of the data items, name: {}, val: {}'.format(type(name).__name__, type(val).__name__)))
		self.smeta = smeta
		self.data = data

	def __str__(self):
		"""String conversion"""
		return ', '.join([': '.join((name, str(self.__getattribute__(name)))) for name in self.__slots__])


class QualitySaver(object):
	"""Quality evaluations saver to the persistent storage"""
	# Max number of the buffered items in the queue that have not been processed
	# before blocking the caller on appending more items
	# Should not be too much to save them into the persistent store on the
	# program termination or any external interruptions
	QUEUE_SIZEMAX = max(128, cpu_count() * 2)  # At least 128 or twice the number of the logical CPUs in the system

	# LATENCY = 1  # Latency in seconds

	# TODO: Efficient multiprocess implementation requires a single instance of the storage to not reload
	# the storage after each creation of a new group or dataset in it. So, group and dataset creation requests
	# should be performed via the queue together with the ordinary data entry creation requests implemented as follows.
	# @staticmethod
	# def __datasaver(qsqueue, syncstorage, active, timeout=None, latency=2.):
	# 	"""Worker process function to save data to the persistent storage
	#
	# 	qsqueue: Queue  - quality saver queue of QEntry items
	# 	syncstorage: h5py.File  - synchronized wrapper of the HDF5 storage
	# 	active: Value('B', lock=False)  - the saver process is operational (the requests can be processed)
	# 	timeout: float  - global operational timeout in seconds, None means no timeout
	# 	latency: float  - latency of the datasaver in sec, recommended value: 1-3 sec
	# 	"""
	# 	# def fetchAndSave():
	#
	# 	tstart = time.perf_counter()  # Global start time
	# 	tcur = tstart  # Start of the current iteration
	# 	while (active.value and (timeout is None or tcur - tstart < timeout)):
	# 		# Fetch and serialize items from the queue limiting their number
	# 		# (can be added in parallel) to avoid large latency
	# 		i = 0
	# 		while not qsqueue.empty() and i < QualitySaver.QUEUE_SIZEMAX:
	# 			i += 1
	# 			# qm = qsqueue.get(True, timeout=latency)  # Measures to be stored
	# 			qm = qsqueue.get_nowait()
	# 			assert isinstance(qm, QEntry), 'Unexpected type of the quality entry: ' + type(qm).__name__
	# 			# Save data elements (entries)
	# 			for metric, mval in  viewitems(qm.data):
	# 				try:
	# 					# Metric is  str (or can be unicode in Python2)
	# 					assert isinstance(mval, float), 'Invalid data type, metric: {}, value: {}'.format(
	# 						type(metric).__name__, type(mval).__name__)
	# 					# Construct dataset name based on the quality measure binary name and its metric name
	# 					# (in case of multiple metrics are evaluated by the executing app)
	# 					dsname = qm.smeta.measure if not metric else _PREFMETR.join((qm.smeta.measure, metric))
	# 					if qm.smeta.ulev:
	# 						dsname += _SUFULEV
	# 					dsname += _EXTQDATASET
	# 					# Open or create the required dataset
	# 					qmgroup = syncstorage.value[qm.smeta.group]
	# 					qmdata = None
	# 					try:
	# 						qmdata = qmgroup[dsname]
	# 					except KeyError:
	# 						# Such dataset does not exist, create it
	# 						nins = 1
	# 						nshf = 1
	# 						nlev = 1
	# 						if not qm.smeta.ulev:
	# 							nins = qmgroup.attrs[SATTRNINS]
	# 							nshf = qmgroup.attrs[SATTRNSHF]
	# 							nlev = qmgroup.parent.attrs[SATTRNLEV]
	# 						qmdata = qmgroup.create_dataset(dsname, shape=(nins, nshf, nlev, QMSRUNS.get(qm.smeta.measure, 1)),
	# 							# 32-bit floating number, checksum (fletcher32)
	# 							dtype='f4', fletcher32=True, fillvalue=np.float32(np.nan), track_times=True)
	# 							# NOTE: Numpy NA (not available) instead of NaN (not a number) might be preferable
	# 							# but it requires latest NumPy versions.
	# 							# https://www.numpy.org/NA-overview.html
	# 							# Numpy NAs (https://docs.scipy.org/doc/numpy-1.14.0/neps/missing-data.html):
	# 							# np.NA,  dtype='NA[f4]', dtype='NA', np.dtype('NA[f4,NaN]')
	# 					# Save data to the storage
	# 					with syncstorage.get_lock():
	# 						qmdata[qm.smeta.iins][qm.smeta.ishf][qm.smeta.ilev][qm.smeta.irun] = mval
	# 				except Exception as err:  #pylint: disable=W0703;  # queue.Empty as err:  # TypeError (HDF5), KeyError
	# 					print('ERROR, saving of {} in {}{}{} failed: {}. {}'.format(mval, qm.smeta.measure,
	# 						'' if not metric else _PREFMETR + metric, '' if not qm.smeta.ulev else _SUFULEV,
	# 						err, traceback.format_exc(5)), file=sys.stderr)
	# 				# alg = apps.require_dataset('Pscan01'.encode(),shape=(0,),dtype=h5py.special_dtype(vlen=bytes),chunks=(10,),maxshape=(None,),fletcher32=True)
	# 				# 	# Allocate chunks of 10 items starting with empty dataset and with possibility
	# 				# 	# to resize up to 500 items (params combinations)
	# 				# 	self.apps[app] = appsdir.require_dataset(app, shape=(0,), dtype=h5py.special_dtype(vlen=_h5str)
	# 				# 		, chunks=(10,), maxshape=(500,))  # , maxshape=(None,), fletcher32=True
	# 				# self.evals = self.storage.require_group('evals')  # Quality evaluations dir (group)
	# 		# Prepare for the next iteration considering the processing latency to reduce CPU loading
	# 		duration = time.perf_counter() - tcur
	# 		if duration < latency:
	# 			time.sleep(latency - duration)
	# 			duration = latency
	# 		tcur += duration
	# 	active.value = False
	# 	if not qsqueue.empty():
	# 		try:
	# 			print('WARNING QualitySaver, {} items remained unsaved in the terminating queue'
	# 				.format(qsqueue.qsize()))
	# 		except NotImplementedError:
	# 			print('WARNING QualitySaver, some items remained unsaved in the terminating queue')
	# 	# Note: qsqueue closing in the worker process (here) causes exception on the QualSaver destruction
	# 	# qsqueue.close()  # Close queue to prevent scheduling another tasks

	def __init__(self, seed, update=False):  # , timeout=None;  algs, qms, nets=None
		"""Creating or open HDF5 storage and prepare for the quality measures evaluations

		Check whether the storage exists, copy/move old storage to the backup and
		create the new one if the storage is not exist.

		seed: uint64  - benchmarking seed, natural number
		update: bool  - update existing storage creating if not exists, or create a new one backing up the existent

		Members:
			storage: h5py.File  - HDF5 storage with synchronized access
				ATTENTION: parallel write to the storage is not supported, i.e. requires synchronization layer
		"""
		# timeout: float  - global operational timeout in seconds, None means no timeout
		# Members:
		# 	_persister: Process  - persister worker process
		# 	queue: Queue  - multiprocess queue whose items are saved (persisted)
		# 	_active: Value('B')  - the storage is operational (the requests can be processed)

		# and (timeout is None or timeout >= 0)
		assert isinstance(seed, int), 'Invalid seed type: {}'.format(type(seed).__name__)
		# Open or init the HDF5 storage
		# self._tstart = time.perf_counter()
		# self.timeout = timeout
		timefmt = '%y%m%d-%H%M%S'  # Start time of the benchmarking, time format: YYMMDD_HHMMSS
		timestamp = time.strftime(timefmt, TIMESTAMP_START)  # Timestamp string
		seedstr = str(seed)
		qmsdir = RESDIR + QMSDIR  # Quality measures directory
		if not os.path.exists(qmsdir):
			os.makedirs(qmsdir)
		# HDF5 Storage: qmeasures_<seed>.h5
		storage = ''.join((qmsdir, 'qmeasures_', seedstr, '.h5'))  # File name of the HDF5.storage
		ublocksize = 512  # HDF5 .userblock size in bytes
		ublocksep = ':'  # Userblock values separator
		# try:
		if os.path.isfile(storage):
			# Read userblock: seed and timestamps, validate new seed and estimate whether
			# there is enought space for one more timestamp
			bcksftime = None
			if update:
				try:
					fstorage = h5py.File(storage, mode='r', driver='core', libver='latest')
					ublocksize = fstorage.userblock_size
					fstorage.close()
				except OSError:
					print('WARNING, can not open the file {}, default userblock size will be used.'.format(
						storage, file=sys.stderr))
				with open(storage, 'r+b') as fstore:  # Open file for R/W in binary mode
					# Note: userblock contains '<seed>:<timestamp1>:<timestamp2>...',
					# where timestamp has timefmt
					ublock = fstore.read(ublocksize).decode().rstrip('\0')
					ubparts = ublock.split(ublocksep)
					if len(ubparts) < 2:
						update = False
						print('ERROR, {} userblock should contain at least 2 items (seed and 1+ timestamp): {}.'
							' The new store will be created.'.format(storage, ublock), file=sys.stderr)
					if update and int(ubparts[0]) != seed:
						update = False
						print('WARNING, update is supported only for the same seed.'
							' Specified seed {} != {} storage seed. New storage will be created.'
							.format(seed, ubparts[0]), file=sys.stderr)
					# Update userblock if possible
					if update:
						if len(ublock) + len(ublocksep) + len(timestamp) <= ublocksize:
							fstore.seek(len(ublock))
							# Note: .encode() is required for the byte stream in Python3
							fstore.write(ublocksep.encode())  # Note: initially userblock is filled with 0
							fstore.write(timestamp.encode())
						else:
							update = False
							print('WARNING, {} can not be updated because the userblock is already full.'
								' A new storage will be created.'.format(storage), file=sys.stderr)
					bcksftime = syncedTime(time.strptime(ubparts[-1], timefmt), lock=False)  # Use last benchmarking start time
			tobackup(storage, False, synctime=bcksftime, move=not update)  # Copy/move to the backup
		elif update:
			update = False
			print('WARNING, the storage does not exist and can not be updated, created:', storage)
		# Create HFD5 storage if required
		if not update:
			# Create the storage, fail if exists ('w-' or 'x')
			fstorage = h5py.File(storage, mode='w-', driver='core', libver='latest', userblock_size=ublocksize)
			ubsize = fstorage.userblock_size  # Actual user block size of the storage
			fstorage.close()
			# Write the userblock
			if (ubsize and len(seedstr) + len(ublocksep) + len(timestamp) <= ubsize):
				with open(storage, 'r+b') as fstore:  # Open file for R/W in binary mode
					fstore.write(seedstr.encode())  # Note: .encode() is required for the byte stream in Python3
					fstore.write(ublocksep.encode())  # Note: initially userblock is filled with 0
					fstore.write(timestamp.encode())
					# Fill remained part with zeros to be sure that userblock is zeroed
					fstore.write(('\0' * (ubsize - (len(seedstr) + len(ublocksep) + len(timestamp)))).encode())
			else:
				raise RuntimeError('ERROR, the userblock creation failed in the {}, userblock_size: {}'
					', initial data size: {} (seed: {}, sep: {}, timestamp:{})'.format(storage, ubsize,
					len(seedstr) + len(ublocksep) + len(timestamp), len(seedstr), len(ublocksep), len(timestamp)))
			# print('> HDF5 storage userblock created: ', seedstr, ublocksep, timestamp)
		# Note: append mode is the default one; core driver is a memory-mapped file, block_size is default (64 Kb)
		# Persistent storage object (file)
		self.storage = h5py.File(storage, mode='a', driver='core', libver='latest', userblock_size=ublocksize)
		# Add attributes if required
		dqrname = 'dims_qms_raw'
		if self.storage.attrs.get(dqrname) is None or update:
			# Describe dataset dimensions
			# Note: the dimension is implicitly omitted in the visualizing table if its size equals to 1
			dims_qms_raw = ('inst', 'shuf', 'levl', 'mrun')
			dqrlen = max((len(s) for s in dims_qms_raw)) + 1
			dqrtype = 'a' + str(dqrlen)  # Zero terminated bytes, fixed length
			# NOTE: the existing attribute is overwritten
			self.storage.attrs.create(dqrname, data=np.array(dims_qms_raw, dtype=dqrtype))
			# shape=(len(dims_qms_raw),), dtype=dqrtype)
			# dims_qms_agg = ('net'): ('avg', 'var', 'num')  # 'dims_qms_agg'

		# except Exception as err:  #pylint: disable=W0703
		# 	print('ERROR, HDF5 storage creation failed: {}. {}'.format(err, traceback.format_exc(5)), file=sys.stderr)
		# 	raise

		# Initialize or update metadata and groups
		# # rescons meta data (h5str array)
		# try:
		# 	self.mrescons = [b.encode() for b in self.storage.value['rescons.inf'][()]]
		# except KeyError:  # IndexError
		# 	self.mrescons = ['ExecTime', 'CPU_time', 'RSS_peak']
		# 	# Note: None in maxshape means resizable, fletcher32 used for the checksum
		# 	self.storage.create_dataset('rescons.inf', shape=(len(self.mrescons),)
		# 		, dtype=h5str, data=[s.decode() for s in self.mrescons], fletcher32=True)  # fillvalue=''
		# # # Note: None in maxshape means resizable, fletcher32 used for the checksum,
		# # # exact used to require shape and type to match exactly
		# # metares = self.storage.require_dataset('rescons.meta', shape=(len(self.mrescons),), dtype=h5str
		# # 	, data=self.mrescons, exact=True, fletcher32=True)  # fillvalue=''
		# #
		# # rescons str to the index mapping
		# self.irescons = {s: i for i, s in enumerate(self.mrescons)}

		# self.queue = None  # Note: the multiprocess queue is created in the enter blocks
		# # The storage is not operational until the queue is created
		# # Note: a shared value for the active state is sufficient, exact synchronization is not required
		# self._active = Value('B', False, lock=False)
		# self._persister = None

	def __call__(self, qm):
		"""Worker process function to save data to the persistent storage

		qm: QEntry  - quality metric (data and metadata) to be saved into the persistent storage
		"""
		assert isinstance(qm, QEntry), 'Unexpected type of the quality entry: ' + type(qm).__name__
		# Save data elements (entries)
		for metric, mval in viewitems(qm.data):
			try:
				# Metric is  str (or can be unicode in Python2)
				assert isinstance(mval, float), 'Invalid data type, metric: {}, value: {}'.format(
					type(metric).__name__, type(mval).__name__)
				# Construct dataset name based on the quality measure binary name and its metric name
				# (in case of multiple metrics are evaluated by the executing app)
				dsname = qm.smeta.measure if not metric else _PREFMETR.join((qm.smeta.measure, metric))
				if qm.smeta.ulev:
					dsname += _SUFULEV
				#print('> dsname: {}, metric: {}, mval: {}; location: {}'.format(dsname, metric, mval, qm.smeta))
				dsname += _EXTQDATASET
				# Open or create the required dataset
				qmgroup = self.storage[qm.smeta.group]
				# qmdata = qmgroup.create_dataset(dsname, shape=(nins, nshf, nlev, QMSRUNS.get(qm.smeta.measure, 1)),
				# 	# 32-bit floating number, checksum (fletcher32), "exact" used to require both shape and type to match exactly
				# 	dtype='f4', exact=True, fletcher32=True, fillvalue=np.float32(np.nan), track_times=True)
				qmdata = None
				try:
					# Note: the out of bound values are omitted in case of update
					qmdata = qmgroup[dsname]
				except KeyError:
					# Such dataset does not exist, create it
					nins = qmgroup.attrs[SATTRNINS]
					nshf = qmgroup.attrs[SATTRNSHF]
					nlev = 1 if qm.smeta.ulev else qmgroup.parent.attrs[SATTRNLEV]
					qmdata = qmgroup.create_dataset(dsname, shape=(nins, nshf, nlev, QMSRUNS.get(qm.smeta.measure, 1)),
						# 32-bit floating number, checksum (fletcher32)
						dtype='f4', fletcher32=True, fillvalue=np.float32(np.nan), track_times=True)
					# NOTE: Numpy NA (not available) instead of NaN (not a number) might be preferable
					# but it requires latest NumPy versions.
					# https://www.numpy.org/NA-overview.html
					# Numpy NAs (https://docs.scipy.org/doc/numpy-1.14.0/neps/missing-data.html):
					# np.NA,  dtype='NA[f4]', dtype='NA', np.dtype('NA[f4,NaN]')

				# Save data to the storage
				# with syncstorage.get_lock():
				# print('>> [{},{},{},{}]{}: {}'.format(qm.smeta.iins, qm.smeta.ishf, qm.smeta.ilev, qm.smeta.irun,
				# 	'' if not qm.smeta.ulev else 'u', mval))
				qmdata[qm.smeta.iins, qm.smeta.ishf, qm.smeta.ilev,
					qm.smeta.irun] = mval
			except Exception as err:  #pylint: disable=W0703;  # queue.Empty as err:  # TypeError (HDF5), KeyError
				print('ERROR, saving of {} into {}{}{}[{},{},{},{}] failed: {}. {}'.format(
					mval, qm.smeta.measure, '' if not metric else _PREFMETR + metric,
					'' if not qm.smeta.ulev else _SUFULEV, qm.smeta.iins, qm.smeta.ishf,
					qm.smeta.ilev, qm.smeta.irun, err, traceback.format_exc(5)), file=sys.stderr)
			# alg = apps.require_dataset('Pscan01'.encode(),shape=(0,),dtype=h5py.special_dtype(vlen=bytes),chunks=(10,),maxshape=(None,),fletcher32=True)
			# 	# Allocate chunks of 10 items starting with empty dataset and with possibility
			# 	# to resize up to 500 items (params combinations)
			# 	self.apps[app] = appsdir.require_dataset(app, shape=(0,), dtype=h5py.special_dtype(vlen=_h5str)
			# 		, chunks=(10,), maxshape=(500,))  # , maxshape=(None,), fletcher32=True
			# self.evals = self.storage.require_group('evals')  # Quality evaluations dir (group)

	def __del__(self):
		"""Destructor"""
		# self._active.value = False
		# try:
		# 	if self.queue is not None:
		# 		try:
		# 			if not self.queue.empty():
		# 				print('WARNING, terminating the persistency layer with {} queued data entries, call stack: {}'
		# 					.format(self.queue.qsize(), traceback.format_exc(5)), file=sys.stderr)
		# 		except OSError:   # The queue has been already closed from another process
		# 			pass
		# 		self.queue.close()  # No more data can be put in the queue
		# 		self.queue.join_thread()
		# 	if self._persister is not None:
		# 		self._persister.join(0)  # Note: timeout is 0 to avoid destructor blocking
		# finally:
		# 	if self.storage is None:
		# 		return
		# 	with self.storage.get_lock():
		# 		if self.storage.value is not None:
		# 			self.storage.close()

		if self.storage is not None:
			self.storage.close()

	def __enter__(self):
		# 	"""Context entrence"""
		# 	self.queue = Queue(self.QUEUE_SIZEMAX)  # Qulity measures persistence queue, data pool
		# 	self._active.value = True
		# 	# __datasaver(qsqueue, active, timeout=None, latency=2.)
		# 	self._persister = Process(target=self.__datasaver, args=(self.queue, self.storage, self._active, self.timeout))
		# 	self._persister.start()
		return self

	def __exit__(self, etype, evalue, tracebk):
		"""Contex exit

		etype  - exception type
		evalue  - exception value
		tracebk  - exception traceback
		"""
		# 	self._active.value = False
		# 	try:
		# 		self.queue.close()  # No more data can be put in the queue
		# 		self.queue.join_thread()
		# 		self._persister.join(None if self.timeout is None else self.timeout - self._tstart)
		# 	finally:
		# 		with self.storage.get_lock():
		# 			if self.storage.value is not None:
		# 				self.storage.value.flush()  # Allow to reuse the instance in several context managers
		# 	# Note: the exception (if any) is propagated if True is not returned here

		if self.storage is not None:
			self.storage.flush()  # Allow to reuse the instance in several context managers


def metainfo(afnmask=None, intrinsic=False, multirun=1):
	"""Set some meta information for the executing evaluation measures

	afnstep: AffinityMask  - affinity mask
	intrinsic: bool  - whether the quality measure is intrinsic and requires input network
		instead of the ground-truth clustering
	multirun: uint8, >= 1  - perform multiple runs of this stochastic quality measure
	"""

	# Note: the metrics producing by the measure can be defined by the execution arguments
	# metrics: list(str)  - quality metrics producing by the measure
	def decor(func):
		"""Decorator returning the original function"""
		assert (afnmask is None or isinstance(afnmask, AffinityMask)) and multirun >= 1 and isinstance(multirun, int), (
			'Invalid arguments, affinity mask type: {}, multirun: {}'.format(type(afnmask).__name__, multirun))
		# QMSRAFN[funcToAppName(func)] = afnmask
		if afnmask is not None and afnmask.afnstep != 1:  # Save only quality measures with non-default affinity
			QMSRAFN[func] = afnmask
		if intrinsic:
			QMSINTRIN.add(func)
		if multirun >= 2:
			# ATTENTION: function name is used to retrieve it from the value from the persister by the qmeasure name
			QMSRUNS[funcToAppName(func.__name__)] = multirun
		return func

	return decor


# def saveQuality(qsqueue, qentry):
# 	"""Save quality entry int the Quality Saver queue
#
# 	Args:
# 		qsqueue: Queue  - quality saver queue
# 		qentry: QEntry  - quality entry to be saved
# 	"""
# 	# Note: multiprocessing Queue is not a Python class, it is a function creating a proxy object
# 	assert isinstance(qentry, QEntry), ('Unexpected type of the arguments, qsqueue: {}, qentry: {}'
# 		.format(type(qsqueue).__name__, type(qentry).__name__))
# 	try:
# 		# Note: evaluators should not be delayed in the main thread
# 		# Anyway qsqueue is buffered and in theory serialization
# 		qsqueue.put_nowait(qentry)
# 	except queue.Full as err:
# 		print('WARNING, the quality entry ({}) saving is cancelled because of the busy serialization queue: {}'
# 			.format(str(qentry), err, file=sys.stderr))


# Note: default AffinityMask is 1 (logical CPUs, i.e. hardware threads)
def qmeasure(qmapp, workdir=UTILDIR):
	"""Quality Measure exutor decorator

	qmapp: str  - quality measure application (binary) name (located in the ./utils dir)
	workdir: str  - current working directory from which the quality measure binare is called
	"""

	def wrapper(qmsaver):  # Actual decorator for the qmsaver func(Job)
		"""Actual decorator of the quality measure parcing saving function

		qmsaver: callable(job: Job)  - parcing and saving function of the quality measure,
			used as a Job.ondone() callback
		"""
		qmsname = None  # Name of the wrapping callable object (function or class instance)
		try:
			qmsname = qmsaver.__name__
		except AttributeError:  # The callable is not a function, so it should be a class object
			qmsname = qmsaver.__class__

		def executor(execpool, save, smeta, qparams, cfpath, inpfpath, asym=False, timeout=0, seed=None
		, task=None, workdir=workdir, revalue=True):
			"""Quality measure executor

			execpool: ExecPool  - execution pool
			save: QualitySaver or callable proxy to its persistance routine  - quality results saving function or functor
			smeta: SMeta - serialization meta data
			qparams: iterable(str)  - quality measures parameters (arguments excluding the clustering and network files)
			cfpath: str  - file path of the clustering to be evaluated
			inpfpath: str  - input dataset file path (ground-truth / input network for the ex/in-trinsic quality measure, includes network instance part if any)
			asym: bool  - whether the input network is asymmetric (directed, specified by arcs)
			timeout: uint  - execution timeout in seconds, 0 means infinity
			seed: uint  - seed for the stochastic qmeasures
			task: Task  - owner (super) task
			workdir: str  - working directory of the quality measure (qmeasure location)
			revalue: bool  - whether to revalue the existent results or omit such evaluations
				calculating and saving only the values which are not present in the dataset.
				NOTE: The default value is True because of the straight forward out of the box implementation.
				ATTENTION: Not all quality measure implementations might support early omission
					of the calculations on revalue=False, in which case a warning should be issued.

			return jobsnum: uint  - the number of started jobs
			"""
			if not revalue:
				# TODO: implement early exit on qualsaver.valueExists(smeta, metrics),
				# where metrics are provided by the quality measure app by it's qparams
				staticTrace(qmsname, 'Omission of the existent results formation is not supported yet')
			# qsqueue: Queue  - multiprocess queue of the quality results saver (persister)
			assert execpool and callable(save) and isinstance(smeta, SMeta
			) and isinstance(cfpath, str) and isinstance(inpfpath, str) and (seed is None or isinstance(seed, int)
			) and (task is None or isinstance(task, Task)), (
			'Invalid arguments, execpool type: {}, save() type: {}, smeta type: {}, cfpath type: {},'
			' inpfpath type: {}, timeout: {}, seed: {}, task type: {}'.format(
			type(execpool).__name__, type(save).__name__, type(smeta).__name__, type(cfpath).__name__,
			type(inpfpath).__name__, timeout, seed, type(task).__name__))

			# The task argument name already includes: QMeasure / BaseNet#PathId / Alg,
			# so here smeta parts and qparams should form the job name for the full identification of the executing job
			# Note: HDF5 uses Unicode for the file name and ASCII/Unicode for the group names
			algname, basenetp = smeta.group[1:].split('/')  # Omit the leading '/'; basenetp includes pathid
			# Note that evaluating file name might significantly differ from the network name, for example `tp<id>` produced by OSLOM
			cfname = os.path.splitext(os.path.split(cfpath)[1])[0]  # Evaluating file name (without the extension)
			measurep = SEPPARS.join((smeta.measure, _SEPQARGS.join(qparams)))  # Quality measure suffixed with its parameters
			taskname = _SEPQMS.join((cfname, measurep))

			# Evaluate relative size of the clusterings
			# Note: xmeasures takes inpfpath as the ground-truth clustering, so the asym parameter is not actual here
			clsize = os.path.getsize(cfpath) + os.path.getsize(inpfpath)

			# Define path to the logs relative to the root dir of the benchmark
			logsdir = ''.join((RESDIR, algname, '/', QMSDIR, basenetp, '/'))
			# Note: backup is not performed since it should be performed at most once for all logs in the logsdir
			# (staticExec could be used) and only if the logs are rewriting but they are appended.
			# The backup is not convenient here for multiple runs on various networks to get aggregated results
			if not os.path.exists(logsdir):
				os.makedirs(logsdir)
			errfile = taskname.join((logsdir, EXTERR))
			logfile = taskname.join((logsdir, EXTLOG))

			# Note: without './' relpath args do not work properly for the binaries located in the current dir
			relpath = lambda path: './' + os.path.relpath(path, workdir)  # Relative path to the specified basedir
			# Evaluate relative paths
			# xtimebin = './exectime'  # Note: relpath(UTILDIR + 'exectime') -> 'exectime' does not work, it requires leading './'
			xtimebin = relpath(UTILDIR + 'exectime')
			xtimeres = relpath(''.join((RESDIR, algname, '/', QMSDIR, measurep, EXTRESCONS)))

			# The task argument name already includes: QMeasure / BaseNet#PathId / Alg
			# Note: xtimeres does not include the base network name, so it should be included into the listed taskname,
			args = [xtimebin, '-o=' + xtimeres, ''.join(('-n=', basenetp, SEPNAMEPART, cfname)),
				'-s=/etime_' + measurep, './' + qmapp]
			if qparams:
				args += qparams
			# Note: use first the ground-truth or network file and then the clustering file to perform sync correctly
			# for the xmeaseres (gecmi and onmi select the most reasonable direction automatically)
			args += (relpath(inpfpath), relpath(cfpath))
			# print('> Starting Xmeasures with the args: ', args)
			# print('> Starting {} for: {}, {}'.format(qmsname, args[-2], args[-1]))
			execpool.execute(Job(name=taskname, workdir=workdir, args=args, timeout=timeout,
				ondone=qmsaver, params={'save': save, 'smeta': smeta},
				# Note: poutlog indicates the output log file that should be formed from the PIPE output
				task=task, category=measurep, size=clsize, stdout=PIPE, stderr=errfile, poutlog=logfile))
			return 1

		executor.__name__ = qmsname
		return executor

	return wrapper


def qmsaver(job):
	"""Default quality measure parser and serializer, used as Job ondone() callback

	job  - executed job, whose params contain:
		save: callable  - save(QEntry) routine to the persistant storage
		smeta: SMeta  - metadata identifying location of the saving values in the storage dataset
	"""
	if not job.pipedout:
		# Note: any notice is redundant here since everything is automatically logged
		# to the Job log (at least the timestamp if the log body itself is empty)
		return
	save = job.params['save']
	smeta = job.params['smeta']
	# xmeasures output is performed either in the last string with metrics separated with ':'
	# from their values and {',', ';'} from each other, where Precision and Recall of F1_labels
	# are parenthesized.
	# The output is performed in 2 last strings only for a single measure with a single value,
	# where the measure name (with possible additional description) is on the pre-last string.
	#
	# Define the number of strings in the output counting the number of words in the last string
	# Identify index of the last non-empty line
	qmres = job.pipedout.rstrip().splitlines()[-2:]  # Fetch last 2 non-empty lines as a list(str)
	# print('Value line: {}, len: {}, sym1: {}'.format(qmres[-1], len(qmres[-1]), ord(qmres[-1][0])))
	if len(qmres[-1].split(None, 1)) == 1:
		# Metric name is None (the same as binary name) if not specified explicitly
		name = None if len(qmres) == 1 else qmres[0].split(None, 1)[0].rstrip(':')  # Omit ending ':' if any
		val = qmres[-1]  # Note: index -1 corresponds to either 0 or 1
		try:
			# qsqueue.put(QEntry(smeta, {name: float(val)}))
			# # , block=True, timeout=None if not timeout else max(0, timeout - (time.perf_counter() - job.tstart)))
			# saveQuality(qsqueue, QEntry(smeta, {name: float(val)}))
			# print('> Parsed data (single) from "{}", name: {}, val: {}; qmres: {}'.format(
			# 	' '.join(('' if len(qmres) == 1 else qmres[0], qmres[-1])), name, val, qmres))
			save(QEntry(smeta, {name: float(val)}))
		except ValueError as err:
			print('ERROR, metric "{}" serialization discarded of the job "{}" because of the invalid value format: {}. {}'
				.format(name, job.name, val, err), file=sys.stderr)
		# except queue.Full as err:
		# 	print('WARNING, results serialization discarded by the Job "{}" timeout'.format(job.name))
		return
	# Parse multiple names of the metrics and their values from the last string: <metric>: <value>{,;} ...
	# Note: index -1 corresponds to either 0 or 1
	metrics = [qmres[-1]]
	# Example of the parsing line: "F1_labels: <val> (Precision: <val>, ...)"
	for sep in ',;(':
		smet = []
		for m in metrics:
			smet.extend(m.split(sep))
		metrics = smet
	data = {}  # Serializing data
	for mt in metrics:
		name, val = mt.split(':', 1)
		try:
			data[name.lstrip()] = float(val.rstrip(' \t)'))
			# print('> Parsed data from "{}", name: {}, val: {}'.format(mt, name.lstrip(), data[name.lstrip()]))
		except ValueError as err:
			print('ERROR, metric "{}" serialization discarded of the job "{}" because of the invalid value format: {}. {}'
				.format(name, job.name, val, err), file=sys.stderr)
	if data:
		# saveQuality(qsqueue, QEntry(smeta, data))
		save(QEntry(smeta, data))


@qmeasure('xmeasures')
def execXmeasures(job):
	"""xmeasures  - various extrinsic quality measures"""
	qmsaver(job)


# Fully defined quality measure executor
# # Note: default AffinityMask is 1 (logical CPUs, i.e. hardware threads)
# def execXmeasures(execpool, save, smeta, qparams, cfpath, inpfpath, asym=False
# , timeout=0, seed=None, task=None, workdir=UTILDIR, revalue=True):
# 	"""Quality measure executor
#
# 	xmeasures  - various extrinsic quality measures
#
# 	execpool: ExecPool  - execution pool
# 	save: QualitySaver or callable proxy to its persistance routine  - quality results saving function or functor
# 	smeta: SMeta - serialization meta data
# 	qparams: iterable(str)  - quality measures parameters (arguments excluding the clustering and network files)
# 	cfpath: str  - file path of the clustering to be evaluated
# 	inpfpath: str  - input dataset file path (ground-truth / input network for the ex/in-trinsic quality measure)
# 	asym: bool  - whether the input network is asymmetric (directed, specified by arcs)
# 	timeout: uint  - execution timeout in seconds, 0 means infinity
# 	seed: uint  - seed for the stochastic qmeasures
# 	task: Task  - owner (super) task
# 	workdir: str  - working directory of the quality measure (qmeasure location)
# 	revalue: bool  - whether to revalue the existent results or omit such evaluations
# 		calculating and saving only the values which are not present in the dataset.
# 		NOTE: The default value is True because of the straight forward out of the box implementation.
# 		ATTENTION: Not all quality measure implementations might support early omission
# 			of the calculations on revalue=False, in which case a warning should be issued.
#
# 	return jobsnum: uint  - the number of started jobs
# 	"""
# 	if not revalue:
# 		# TODO: implement early exit on qualsaver.valueExists(smeta, metrics),
# 		# where metrics are provided by the quality measure app by it's qparams
# 		staticTrace('execXmeasures', 'Omission of the existent results is not supported yet')
# 	# qsqueue: Queue  - multiprocess queue of the quality results saver (persister)
# 	assert execpool and callable(save) and isinstance(smeta, SMeta
# 		) and isinstance(cfpath, str) and isinstance(inpfpath, str) and (seed is None
# 		or isinstance(seed, int)) and (task is None or isinstance(task, Task)), (
# 		'Invalid arguments, execpool type: {}, save() type: {}, smeta type: {}, cfpath type: {},'
# 		' inpfpath type: {}, timeout: {}, seed: {}, task type: {}'.format(type(execpool).__name__,
# 		type(save).__name__, type(smeta).__name__, type(cfpath).__name__, type(inpfpath).__name__,
# 		timeout, seed, type(task).__name__))
#
# 	def saveEvals(job):
# 		"""Job ondone() callback to persist evaluated quality measurements"""
# 		if not job.pipedout:
# 			# Note: any notice is redundant here since everything is automatically logged
# 			# to the Job log (at least the timestamp if the log body itself is empty)
# 			return
# 		save = job.params['save']
# 		smeta = job.params['smeta']
# 		# xmeasures output is performed either in the last string with metrics separated with ':'
# 		# from their values and {',', ';'} from each other, where Precision and Recall of F1_labels
# 		# are parenthesized.
# 		# The output is performed in 2 last strings only for a single measure with a single value,
# 		# where the measure name (with possible additional description) is on the pre-last string.
# 		#
# 		# Define the number of strings in the output counting the number of words in the last string
# 		# Identify index of the last non-empty line
# 		qmres = job.pipedout.rstrip().splitlines()[-2:]  # Fetch last 2 non-empty lines as a list(str)
# 		# print('Value line: {}, len: {}, sym1: {}'.format(qmres[-1], len(qmres[-1]), ord(qmres[-1][0])))
# 		if len(qmres[-1].split(None, 1)) == 1:
# 			# Metric name is None (the same as binary name) if not specified explicitly
# 			name = None if len(qmres) == 1 else qmres[0].split(None, 1)[0].rstrip(':')  # Omit ending ':' if any
# 			val = qmres[-1]  # Note: index -1 corresponds to either 0 or 1
# 			try:
# 				# qsqueue.put(QEntry(smeta, {name: float(val)}))
# 				# # , block=True, timeout=None if not timeout else max(0, timeout - (time.perf_counter() - job.tstart)))
# 				# saveQuality(qsqueue, QEntry(smeta, {name: float(val)}))
# 				# print('> Parsed data (single) from "{}", name: {}, val: {}; qmres: {}'.format(
# 				# 	' '.join(('' if len(qmres) == 1 else qmres[0], qmres[-1])), name, val, qmres))
# 				save(QEntry(smeta, {name: float(val)}))
# 			except ValueError as err:
# 				print('ERROR, metric "{}" serialization discarded of the job "{}" because of the invalid value format: {}. {}'
# 					.format(name, job.name, val, err), file=sys.stderr)
# 			# except queue.Full as err:
# 			# 	print('WARNING, results serialization discarded by the Job "{}" timeout'.format(job.name))
# 			return
# 		# Parse multiple names of the metrics and their values from the last string: <metric>: <value>{,;} ...
# 		# Note: index -1 corresponds to either 0 or 1
# 		metrics = [qmres[-1]]
# 		# Example of the parsing line: "F1_labels: <val> (Precision: <val>, ...)"
# 		for sep in ',;(':
# 			smet = []
# 			for m in metrics:
# 				smet.extend(m.split(sep))
# 			metrics = smet
# 		data = {}  # Serializing data
# 		for mt in metrics:
# 			name, val = mt.split(':', 1)
# 			try:
# 				data[name.lstrip()] = float(val.rstrip(' \t)'))
# 				# print('> Parsed data from "{}", name: {}, val: {}'.format(mt, name.lstrip(), data[name.lstrip()]))
# 			except ValueError as err:
# 				print('ERROR, metric "{}" serialization discarded of the job "{}" because of the invalid value format: {}. {}'
# 					.format(name, job.name, val, err), file=sys.stderr)
# 		if data:
# 			# saveQuality(qsqueue, QEntry(smeta, data))
# 			save(QEntry(smeta, data))
#
# 	# The task argument name already includes: QMeasure / BaseNet#PathId / Alg,
# 	# so here smeta parts and qparams should form the job name for the full identification of the executing job
# 	# Note: HDF5 uses Unicode for the file name and ASCII/Unicode for the group names
# 	algname, basenetp = smeta.group[1:].split('/')  # Omit the leading '/'; basenetp includes pathid
# 	# Note that evaluating file name might significantly differ from the network name, for example `tp<id>` produced by OSLOM
# 	cfname = os.path.splitext(os.path.split(cfpath)[1])[0]  # Evaluating file name (without the extension)
# 	measurep = SEPPARS.join((smeta.measure, _SEPQARGS.join(qparams)))  # Quality measure suffixed with its parameters
# 	taskname = _SEPQMS.join((cfname, measurep))
#
# 	# Evaluate relative size of the clusterings
# 	# Note: xmeasures takes inpfpath as the ground-truth clustering, so the asym parameter is not actual here
# 	clsize = os.path.getsize(cfpath) + os.path.getsize(inpfpath)
#
# 	# Define path to the logs relative to the root dir of the benchmark
# 	logsdir = ''.join((RESDIR, algname, '/', QMSDIR, basenetp, '/'))
# 	# Note: backup is not performed since it should be performed at most once for all logs in the logsdir
# 	# (staticExec could be used) and only if the logs are rewriting but they are appended.
# 	# The backup is not convenient here for multiple runs on various networks to get aggregated results
# 	if not os.path.exists(logsdir):
# 		os.makedirs(logsdir)
# 	errfile = taskname.join((logsdir, EXTERR))
# 	logfile = taskname.join((logsdir, EXTLOG))
#
# 	relpath = lambda path: './' + os.path.relpath(path, workdir)  # Relative path to the specified basedir
# 	# Evaluate relative paths
# 	xtimebin = './exectime'  # Note: relpath(UTILDIR + 'exectime') -> 'exectime' does not work, it requires leading './'
# 	xtimeres = relpath(''.join((RESDIR, algname, '/', QMSDIR, measurep, EXTRESCONS)))
#
# 	# The task argument name already includes: QMeasure / BaseNet#PathId / Alg
# 	# Note: xtimeres does not include the base network name, so it should be included into the listed taskname,
# 	args = [xtimebin, '-o=' + xtimeres, ''.join(('-n=', basenetp, SEPNAMEPART, cfname)), '-s=/etime_' + measurep, './xmeasures']
# 	if qparams:
# 		args += qparams
# 	args += (relpath(cfpath), relpath(inpfpath))
# 	# print('> Starting Xmeasures with the args: ', args)
# 	execpool.execute(Job(name=taskname, workdir=workdir, args=args, timeout=timeout
# 		, ondone=saveEvals, params={'save': save, 'smeta': smeta}
# 		# Note: poutlog indicates the output log file that should be formed from the PIPE output
# 		, task=task, category=measurep, size=clsize, stdout=PIPE, stderr=errfile, poutlog=logfile))
# 	return 1


@qmeasure('gecmi')
@metainfo(afnmask=AffinityMask(AffinityMask.NODE_CPUS, first=False), multirun=3)  # Note: multirun requires irun
def execGnmi(job):
	"""gnmi (gecmi)  - Generalized Normalized Mutual Information"""
	qmsaver(job)


@qmeasure('onmi')
def execOnmi(job):
	"""onmi  - Overlapping NMI"""
	qmsaver(job)


# @qmeasure('daoc', workdir=ALGSDIR + 'daoc/')
# @metainfo(intrinsic=True)  # Note: intrinsic causes interpretation of ifname as inpnet and requires netparams
# def execImeasures(job):
@metainfo(intrinsic=True)  # Note: intrinsic causes interpretation of ifname as inpnet and requires netparams
def execImeasures(execpool, save, smeta, qparams, cfpath, inpfpath, asym=False, timeout=0
, seed=None, task=None, workdir=ALGSDIR + 'daoc/', revalue=True):
	"""imeasures (proxy for DAOC)  - executor of some intrinsic quality measures

	execpool: ExecPool  - execution pool
	save: QualitySaver or callable proxy to its persistance routine  - quality results saving function or functor
	smeta: SMeta - serialization meta data
	qparams: iterable(str)  - quality measures parameters (arguments excluding the clustering and network files)
	cfpath: str  - file path of the clustering to be evaluated
	inpfpath: str  - input dataset file path (ground-truth / input network for the ex/in-trinsic quality measure)
	asym: bool  - whether the input network is asymmetric (directed, specified by arcs)
	timeout: uint  - execution timeout in seconds, 0 means infinity
	seed: uint  - seed for the stochastic qmeasures
	task: Task  - owner (super) task
	workdir: str  - working directory of the quality measure (qmeasure location)
	revalue: bool  - whether to revalue the existent results or omit such evaluations
		calculating and saving only the values which are not present in the dataset.
		NOTE: The default value is True because of the straight forward out of the box implementation.
		ATTENTION: Not all quality measure implementations might support early omission
			of the calculations on revalue=False, in which case a warning should be issued.

	return jobsnum: uint  - the number of started jobs
	"""
	if not revalue:
		# TODO: implement early exit on qualsaver.valueExists(smeta, metrics),
		# where metrics are provided by the quality measure app by it's qparams
		staticTrace('Imeasures', 'Omission of the existent results is not supported yet')
	# qsqueue: Queue  - multiprocess queue of the quality results saver (persister)
	assert execpool and callable(save) and isinstance(smeta, SMeta
		) and isinstance(cfpath, str) and isinstance(inpfpath, str) and (
		seed is None or isinstance(seed, int)) and (task is None or isinstance(task, Task)), (
		'Invalid arguments, execpool type: {}, save() type: {}, smeta type: {}, cfpath type: {},'
		' inpfpath type: {}, timeout: {}, seed: {}, task type: {}'.format(
		type(execpool).__name__, type(save).__name__, type(smeta).__name__, type(cfpath).__name__,
		type(inpfpath).__name__, timeout, seed, type(task).__name__))

	# The task argument name already includes: QMeasure / BaseNet#PathId / Alg,
	# so here smeta parts and qparams should form the job name for the full identification of the executing job
	# Note: HDF5 uses Unicode for the file name and ASCII/Unicode for the group names
	algname, basenetp = smeta.group[1:].split('/')  # Omit the leading '/'; basenetp includes pathid
	# Note that evaluating file name might significantly differ from the network name, for example `tp<id>` produced by OSLOM
	cfname = os.path.splitext(os.path.split(cfpath)[1])[0]  # Evaluating file name (without the extension)
	measurep = SEPPARS.join((smeta.measure, _SEPQARGS.join(qparams)))  # Quality measure suffixed with its parameters
	taskname = _SEPQMS.join((cfname, measurep))

	# Evaluate relative size of the clusterings
	# Note: xmeasures takes inpfpath as the ground-truth clustering, so the asym parameter is not actual here
	clsize = os.path.getsize(cfpath) + os.path.getsize(inpfpath)

	# Define path to the logs relative to the root dir of the benchmark
	logsdir = ''.join((RESDIR, algname, '/', QMSDIR, basenetp, '/'))
	# Note: backup is not performed since it should be performed at most once for all logs in the logsdir
	# (staticExec could be used) and only if the logs are rewriting but they are appended.
	# The backup is not convenient here for multiple runs on various networks to get aggregated results
	if not os.path.exists(logsdir):
		os.makedirs(logsdir)
	errfile = taskname.join((logsdir, EXTERR))
	logfile = taskname.join((logsdir, EXTLOG))

	# Note: without './' relpath args do not work properly for the binaries located in the current dir
	relpath = lambda path: './' + os.path.relpath(path, workdir)  # Relative path to the specified basedir
	# Evaluate relative paths
	# xtimebin = './exectime'  # Note: relpath(UTILDIR + 'exectime') -> 'exectime' does not work, it requires leading './'
	xtimebin = relpath(UTILDIR + 'exectime')
	xtimeres = relpath(''.join((RESDIR, algname, '/', QMSDIR, measurep, EXTRESCONS)))

	# The task argument name already includes: QMeasure / BaseNet#PathId / Alg
	# Note: xtimeres does not include the base network name, so it should be included into the listed taskname,
	args = [xtimebin, '-o=' + xtimeres, ''.join(('-n=', basenetp, SEPNAMEPART, cfname)),
		'-s=/etime_' + measurep, './daoc']
	for qp in qparams:
		if qp.startswith('-e'):  #  Append filename of the evaluating clsutering
			qp = '='.join((qp, relpath(cfpath)))
		args.append(qp)
	# Note: use first the ground-truth or network file and then the clustering file to perform sync correctly
	# for the xmeaseres (gecmi and onmi select the most reasonable direction automatically)
	args.append(relpath(inpfpath))
	# print('> Starting Xmeasures with the args: ', args)
	# print('> Starting {} for: {}, {}'.format('Imeasures', args[-2], args[-1]))
	execpool.execute(Job(name=taskname, workdir=workdir, args=args, timeout=timeout,
		ondone=qmsaver, params={'save': save, 'smeta': smeta},
		# Note: poutlog indicates the output log file that should be formed from the PIPE output
		task=task, category=measurep, size=clsize, stdout=PIPE, stderr=errfile, poutlog=logfile))
	return 1


class ValAcc(object):
	"""Values accumulator

	>>> str(ValAcc().avg()) == 'nan'
	True
	>>> acc = ValAcc(); acc.add(1); acc.add(3.6); acc.avg() == 2.3
	True
	"""
	__slots__ = ('nans', 'num', 'sum')

	def __init__(self):
		"""Initialization of the accumulator

		Attributes:
		nans: int >= 0  - the number of processed NAN values
		num: int >= 0  - the number of processed non-NAN values
		sum: float  - sum of the non-NAN values
		"""
		self.nans = 0
		self.num = 0
		self.sum = 0.

	def add(self, val):
		"""Add value to the accumulator

		val: number  - value to be added
		"""
		assert isinstance(val, Number), 'Unexpected type of val: ' + type(val).__name__
		if not math.isnan(val):
			self.num += 1
			self.sum += val
		else:
			self.nans += 1

	def avg(self):
		"""Average value"""
		# Note: explicit type is required for correct evaluation in Python2
		return np.nan if not self.num else float(self.sum) / self.num

	def reset(self):
		"""Reset accumulation"""
		self.nans = 0
		self.num = 0
		self.sum = 0.

	def __str__(self):
		"""String conversion"""
		return ', '.join([': '.join((name, str(self.__getattribute__(name)))) for name in self.__slots__])


class VarAcc(ValAcc):
	"""Variance accumulator
	var = E[(X - E[X])^2] = E[X^2] - E[X]^2 = sum(x_i^2)/n - (sum(x_i)/n)^2

	>>> str(VarAcc().sd()) == 'nan'
	True
	>>> acc = VarAcc(); acc.add(1); acc.add(3.6); round(acc.sd(), 2) == 1.3
	True
	"""
	__slots__ = ('sum2',)

	def __init__(self):
		"""Initialization of the accumulator

		Attributes:
		sum2: float  - sum of the squared values
		"""
		super(VarAcc, self).__init__()
		self.sum2 = 0.

	def add(self, val):
		"""Add value to the accumulator

		val: number  - value to be added
		"""
		super(VarAcc, self).add(val)
		if not math.isnan(val):
			self.sum2 += val * val

	def sd(self):
		"""Standard deviation"""
		return np.nan if not self.num else math.sqrt((self.sum2 - float(self.sum) / self.num * self.sum) / self.num)

	def reset(self):
		"""Reset accumulation"""
		super(VarAcc, self).reset()
		self.sum2 = 0.

	def __str__(self):
		"""String conversion"""
		return ', '.join([': '.join((name, str(self.__getattribute__(name))))
			for name in itertools.chain(super(VarAcc, self).__slots__, self.__slots__)])


class NamedList(object):
	"""Named list (array) container

	The values are stored in the order of their adding and can be fetched by name
	"""

	# Used for to form networks enum (named array)

	def __init__(self, keys=None):
		"""Container initialization

		keys: iterable  - initial keys, the order is retained

		Internal attributes
		kinds: dict(obj, int >= 0)  - mapping of a key to the respective value index
		values: list(obj)  - values indexed buy the key
		"""
		# Track the order of items
		self.kinds = {} if not keys else {k: i for i, k in enumerate(keys)}
		self.values = [None] * len(self.kinds)
		assert len(self.values) == len(self.kinds), 'Items number should correspond to the keys number'

	def insert(self, key, val):
		"""Insert a key-value pair overwriting the existing if any"""
		size = len(self.kinds)
		i = self.kinds.setdefault(key, size)  # Return the value if exists otherwise set and return the value
		if i < size:
			self.values[i] = val
		else:
			self.values.append(val)
			assert len(self.values) == len(self.kinds
				), 'The number of extended items should correspond to the keys number'

	def get(self, key, default=None):
		"""Get value by the name

		key: obj  - name of the value to be fetched
		default: obj = None  - default value to be returned if the name is not present in the container
		"""
		i = self.get(key)
		return default if i is None else self.values[i]

	def keys(self):
		"""Keys in the order of values (items insertion)"""
		res = [None] * len(self.kinds)
		for k, i in viewitems(self.kinds):
			res[i] = k
		return res

	def __len__(self):
		"""Returns the number of elements in the container"""
		assert len(self.values) == len(self.kinds), 'The number of extended items should correspond to the keys number'
		return len(self.values)


QValStat = namedtuple('QValStat', 'avg sd conf')
"""Quality value extended with the statistical information

avg: float4 (32 bit float)  - average value
sd: float4  - standard deviation of the value
conf: float4  - confidance (rins*rshf*rrun)
"""


def scalar(val):
	"""Return the scalar value (number) from itself or from the NumPy array of size 1"""
	assert isinstance(val, Number) or (isinstance(val, np.ndarray) and val.size == 1
		), 'Unexpected argument type or shape: {}, size: {}'.format(type(val).__name__, val.size)
	return val if not isinstance(val, np.ndarray) else val.item(0)


def aggeval(aggevals, nets, qaggopts, exclude, qmsname, revalue=False, maxins=0):
	"""Aggregate evaluation results from the specified HDF5 storage

	aggevals: dict(qmeasure: dict(algname: NamedList(netname: QValStat)))
		- resulting aggregated evaluations to be extended, where the netname includes pathid if any
		NOTE: Each algorithm has own indexing and number of networks
	nets: set(str)  - resulting networks present in any of the processed algorithms or None
	qaggopts: iterable(QAggOpt)  - quality aggregation options (filter), empty or None means aggregate everything
	exclude: bool  - qaggopts specify items to be excluded from the aggregation instead of the aggregating items
	qmsname: str  - path of the HDF5 quality measures storage to be aggregated
	revalue: bool  - whether to revalue (reaggregate) the existent results or omit such evaluations
		calculating and saving only the absent values in the dataset,
		actual only for the update flag set
	maxins: int >= 0  - max number of instances to process, 0 means all
	"""
	# Check types to validate positional arguments
	assert isinstance(qmsname, str) and isinstance(revalue, bool) and isinstance(maxins, int), (
		'Unexpected types of the arguments, qmsname: {}, revalue: {}, maxins: {}'.format(
		type(qmsname).__name__, type(revalue).__name__, type(maxins).__name__))
	# Define type of the aggregation filtering, which should be the same for all options
	if exclude and not qaggopts:  # Exclude everything, i.e. nothing to be aggregated
		print('WARNING, the aggregation is specified to be excluded, nothing to be done')
		return
	fltout = exclude
	# Index aggregation filters by the algorithm names
	aflts = None if not qaggopts else {flt.alg: flt for flt in qaggopts}
	# Open HDF5 storage of the resulting quality measures
	# HDF5 Storage: qmeasures_<seed>.h5
	# qmsdir = RESDIR + QMSDIR  # Quality measures directory
	print('Opening for the aggregation', qmsname)
	qmeasures = h5py.File(qmsname, mode='r', driver='core', libver='latest')
	print('Aggregating', qmeasures.name)
	avgres = ValAcc()  # Average resulting value over all instances
	avgsd = ValAcc()  # Average standard deviation over all instances
	nansfavg = ValAcc()  #  Average number of NAN shuffles and runs for the instances
	# Shuffles statistics
	varshf = VarAcc()  # Variance over the instance shuffles
	avgrshf = ValAcc()  # Average ration of the NAN shuffles (used to evaluate confidence of the results)
	avgrun = ValAcc() # Average value of measure for multiple runs of the evaluation app
	for galg in viewvalues(qmeasures):
		alg = os.path.split(galg.name)[1]
		aflt = None if not aflts else aflts.get(alg)  # Algorithm aggregation filter
		# Empty inclusive filtering should include everything
		if aflt is None and not fltout:
			fltout = not fltout
		# Filter out algorithms listed in the exclusive filter or not listed in the inclusive filter
		if (fltout is not None) and (
		(fltout and aflt and not aflt.nets and not aflt.msrs) or (not fltout and not aflt)):
			#print('> Omitted by the filtering, fltout: {}, aflt: {}'.format(fltout, aflt))
			continue
		for gnet in viewvalues(galg):
			net = os.path.split(gnet.name)[1]
			# Consider also network wildcard matching if any
			netmatch = None
			if (fltout is not None) and (aflt and aflt.nets):
				netmatch = False
				for ntw in aflt.nets:  # Network name wildcard
					if fnmatch.fnmatch(net, ntw):
						netmatch = True
						break
				if not aflt.msrs and ((netmatch and fltout) or (not netmatch and not fltout)):
					#print('> Omitted by the filtering 2, fltout: {}, aflt: {}, netmatch: {}'
					#	.format(fltout, aflt, netmatch))
					continue
			for dmsr in viewvalues(gnet):
				# Add result to the aggevals
				# a) considering '+u' suffix of the unified representative clusters
				# 	forming a single level from the multi-lev clustering and
				# b) moving it to the algorithm name
				msr = os.path.splitext(os.path.split(dmsr.name)[1])[0]
				assert msr, 'The metric name should be valid'
				if fltout is not None and aflt and aflt.msrs:
					match = False
					# Consider measure prefix matching if any
					if netmatch is not False:
						for mpr in aflt.msrs:  # Measure prefix
							if msr.startswith(mpr):
								match = True
								break
					if (match and fltout) or (not match and not fltout):
						#print('> Omitted by the filtering 3, fltout: {}, aflt: {}, netmatch: {}'
						#	.format(fltout, aflt, match))
						continue
				# Identify whether the quality measure dataset multilevel and has multiple runs:
				# (iinst)[(ishuf)][(ilev)][(qmirun)]: float4
				i = 3
				mrun = len(dmsr.shape) >= i + 1 and dmsr.shape[i] >= 2
				i -= 1
				mlev = mrun or (len(dmsr.shape) >= i + 1 and dmsr.shape[i] >= 2)
				i -= 1
				mshf = mlev or (len(dmsr.shape) >= i + 1 and dmsr.shape[i] >= 2)
				avgres.reset()
				avgsd.reset()
				avgrshf.reset()  # Empty if all shuffles are successfully processed, otherwise the ratio of not NaNs
				nansfavg.reset()
				for iins, inst in enumerate(dmsr):
					if maxins and iins >= maxins:  # Note: iins indexing starts from 0, which corresponds to maxins = 1
						break
					if mshf:
						varshf.reset()
						for shf in inst:
							if mlev:
								# Select level with the highest resulting value
								if mrun:
									vmax = -np.inf
									for lev in shf:
										avgrun.reset()
										for run in lev:
											avgrun.add(run)
											#if alg == 'Daoc' and net == '5K5' and msr == 'Xmeasures:MF1h_w':
											#	print('>> run:', run)
										if vmax < avgrun.avg():
											vmax = avgrun.avg()
									if math.isinf(vmax):
										vmax = np.nan
								else:
									vmax = max(shf)
								varshf.add(scalar(vmax))
								#if alg == 'Daoc' and net == '5K5' and msr == 'Xmeasures:MF1h_w':
								#	print('>> ml shf:', scalar(vmax))
							else:
								varshf.add(scalar(shf))
								#if alg == 'Daoc' and net == '5K5' and msr == 'Xmeasures:MF1h_w':
								#	print('>> u shf:', scalar(shf))
						avgres.add(varshf.avg())
						#if alg == 'Daoc' and net == '5K5' and msr == 'Xmeasures:MF1h_w':
						#	print('>> avgval:', scalar(varshf.avg()))
						avgsd.add(varshf.sd())
						if varshf.nans:
							avgrshf.add(varshf.nans / float(varshf.nans + varshf.num))
					else:
						avgres.add(scalar(inst))
						#if alg == 'Daoc' and net == '5K5' and msr == 'Xmeasures:MF1h_w':
						#	print('>> inst:', scalar(inst))
				# Networks evaluations for each measure and each algorithm
				netsevs = aggevals.setdefault(msr, {}).setdefault(alg, NamedList(keys=nets))
				# conf: float4  - confidance (rins*rshf*rrun)
				qv = QValStat(avgres.avg(), avgsd.avg(), np.nan if not dmsr else  # dmsr.size, len(dmsr)
					avgres.num / float(avgres.nans + avgres.num) * (1 if not avgrshf.num else avgrshf.avg()))
				#if alg == 'Daoc' and net == '5K5' and msr == 'Xmeasures:MF1h_w':
				#	print('> 5K5 qv:', qv)
				netsevs.insert(net, qv)
	if nets is not None:
		nets.union(netsevs.kinds)  # Add keys (network kinds)


def aggEvals(qaggopts, exclude, seed, update=True, revalue=False, plot=False):
	"""Aggregate evaluation results from the HDF5 storages to the dedicated HDF5 storage

	qaggopts: iterable(QAggOpt) or None  - quality aggregation options (filter)
	exclude: bool  - qaggopts specify items to  be excluded from the aggregation instead of the aggregating items
	seed: uint or None  - benchmark seed, natural number. Used to distinguish the target results
		(and parameterize clustering algorithms if required). Aggrege all available qmeasure storages
		if the seed is None.
	update: bool  - update evaluations file (storage of datasets) or create a new one,
		anyway existed evaluations are backed up
	revalue: bool  - whether to revalue (reaggregate) the existent results or omit such evaluations
		calculating and saving only the absent values in the dataset,
		actual only for the update flag set
	"""
	print('Aggregation of the raw quality evaluations started:\n\tqaggopts: {}\n\texclude: {}'
		'\n\tseed: {}\n\tupdate: {}\n\trevalue: {}\n\tplot: {}'.format(qaggopts, exclude, seed, update, revalue, plot))
	if exclude and not qaggopts:
		print('WARNING, all aggregations are specified to be excluded, nothing to be done')
		return
	if plot:  # TODO: implement plotting using mathplotlib
		print('WARNING, plotting has not been implemented yet. Pleasse, copy the aggregated'
			' results to QtiPltot or other plotting system to visualize them')
	qmsdir = RESDIR + QMSDIR  # Quality measures directory
	seedstr = None  # Seed of the aggregating datasets (should be the same)
	qmnbase = 'qmeasures_'
	qmnsuf = '.h5'
	# Aggregated evaluations to be saved
	aggevals = {}  # dict(qmeasure: dict(algname: NamedList(netname: QValStat)))
	nets = set()  # Forming set of networks present in any of the processed algorithms
	maxins = 0  # 1; Note: use default maxins=0 after insuring that values for all instances are correct in the clustering results
	if not revalue:
		# TODO: omit aggregation of the already existent (aggregated) results,
		# which requires first to read the existent aggregated evaluations
		print('WARNING aggEvals(), Omission of the existent results formation is not supported yet')
	if seed is not None:
		seedstr = str(seed)  # Note: only the seed(s) of the aggregating file(s) are meaningful
		qmspath = ''.join((qmsdir, qmnbase, seedstr, qmnsuf))  # File name of the HDF5.storage
		# TODO: Clarify why seedstr is already prefexed in qmspath 
		print('qmspath: ', qmspath)
		aggeval(aggevals, nets, qaggopts, exclude, qmspath, revalue, maxins=maxins)
	else:
		#print('Aggregating', qmsdir,'*'.join((qmnbase, qmnsuf)))
		for qmspath in glob.iglob(qmsdir + '*'.join((qmnbase, qmnsuf))):
			try:
				qmsname = os.path.split(qmspath)[1]
				print('Aggregating', qmsname)
				# Extract seeds and to the added to the userblock of the forming storage
				seed = qmspath[len(qmsdir) + len(qmnbase):len(qmspath) - len(qmnsuf)]  # Note: considered that qmsuf can be empty
				if seedstr is None:
					seedstr = seed
				elif seedstr != seed:
					print('WARNING, the aggregating dataset "{}" is omitted because its seed is distinct'
						' from the aggregation one'.format(qmsname), file=sys.stderr)
					continue
				aggeval(aggevals, nets, qaggopts, exclude, qmspath, revalue, maxins=maxins)
			except Exception as err:  #pylint: disable=W0703
				print('ERROR, quality measures aggregation in {} failed: {}. Discarded. {}'.format(
					qmspath, err, traceback.format_exc(5)), file=sys.stderr)
	# Form the resulting HDF5 storage indicating the number of processed levels (maxins) in the name if used
	aggqpath = ''.join((qmsdir, 'aggqms', '' if not maxins else '%' + str(maxins)
		, '' if seedstr is None else '_' + seedstr, qmnsuf))
	#print('> aggqpath:', aggqpath, ', seedstr:', seedstr)
	if os.path.isfile(aggqpath):
		tobackup(aggqpath, False, move=not update)  # Copy/move to the backup
	try:
		storage = h5py.File(aggqpath, mode='a', driver='core', libver='latest')
	except OSError as err:
		print('ERROR, HDF5 storage "{}" extension failed: {}'.format(aggqpath, err), file=sys.stderr)
		raise
	# Add attributes if required
	if storage.attrs.get('nets') is None or update:
		# List all networks in the utf8 string, the existing attribute is overwritten
		storage.attrs['nets'] = ' '.join(nets)

	# Update/create groups and datasets:
	# /<measure>/<alg>.dat
	# Data format: <net[#pathid]>Enum: QValStat, where QValStat is a tuple(avgares, sdval, conf=rins*rshf*rrun)
	dsupdates = {}  # Wheter the dataset is updating or created/overwritten
	for mtr, qevs in viewitems(aggevals):
		gmtr = storage.require_group(mtr)
		for alg, aevs in viewitems(qevs):
			dsname = alg + '.dat'
			dsupdates.setdefault(dsname, update)
			aggdata = None
			try:
				# Note: the absent values are added in case of update
				aggdata = gmtr[dsname]
			except KeyError:
				dsupdates[dsname] = False
				tdata = np.dtype([(attr, 'f4') for attr in QValStat._fields]) # 32-bit (4 byte) floating numbers
				aggdata = gmtr.create_dataset(dsname, shape=(len(aevs),),
					dtype=tdata,
					#dtype=np.dtype([(attr, 'f4') for attr in QValStat._fields]), # 32-bit (4 byte) floating numbers
					#dtype=np.ndarray(shape=(len(QValStat._fields),), dtype='f4'), # 32-bit (4 byte) floating numbers
					maxshape=(None,), chunks=(8,), #exact=True, # "exact" means that both shape and type should match exactly
					fletcher32=True,
					# fillvalue=QValStat._make([np.float32(np.nan)]*len(QValStat._fields)),
					# fillvalue=tuple([np.float32(np.nan)]*len(QValStat._fields)),
					fillvalue=np.array([np.float32(np.nan)]*len(QValStat._fields), dtype='f4').astype(tdata),
					track_times=True)
			# Extend or reuse the attributes
			dsanets = aggdata.attrs.get('nets')
			# Append the absent networks in the original order and construct mapping of the updated indices
			imap = {}  # src: uint -> dst: uint
			if dsanets is None:
				dnets = aevs.keys()
				aggdata.attrs['nets'] = ' '.join(dnets)
			else:
				dnets = dsanets.split()
				pnets = set(dnets)
				for i, net in enumerate(aevs.keys()):
					if net not in pnets:
						imap[i] = len(dnets)
						dnets.append(net)
				aggdata.attrs['nets'] = ' '.join(dnets)
			# Fill missed values
			if dsupdates[dsname]:
				for i, qv in enumerate(aevs.values):
					iu = imap.get(i, i)  # Updated index
					# if revalue or iu != i:
					#print('> Added val: ', qv)
					aggdata[iu] = qv
			else:
				#print('> Assigned values: ', aevs.values)
				aggdata[...] = aevs.values
	storage.close()  # Explicitly close to flush the file


if __name__ == '__main__':
	# Doc tests execution
	import doctest
	#doctest.testmod()  # Detailed tests output
	flags = doctest.REPORT_NDIFF | doctest.REPORT_ONLY_FIRST_FAILURE
	failed, total = doctest.testmod(optionflags=flags)
	if failed:
		print("Doctest FAILED: {} failures out of {} tests".format(failed, total))
	else:
		print('Doctest PASSED')
