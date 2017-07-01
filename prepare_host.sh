#!/bin/sh
#
# \description  Prepares host environment for the benchmarking execution.
# Note: the made setting are reseted on restart.
#
# \author Artem V L <luart@ya.ru>

# Max number of the opened files in the system
MAX_FILES=1048576
# Max number of the opened files by the process
UL_FILES=32768
# Max swappiness, should be 1..10
MAX_SWPNS=10


if [ `sysctl fs.file-max` -lt $MAX_FILES ]
then
	sudo sysctl -w fs.file-max=$MAX_FILES
	echo "fs.file-max set to $MAX_FILES"
fi

if [ `ulimit -n` -lt $UL_FILES ]
then
	ulimit -n $UL_FILES
	echo "ulimit files set to $UL_FILES"
fi

if [ `sysctl vm.swappiness` $MAX_SWPNS ]
then
	sudo sysctl -w vm.swappiness=$MAX_SWPNS
	echo "vm.swappiness set to $MAX_SWPNS"
fi