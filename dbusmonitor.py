#!/usr/bin/env python
# -*- coding: utf-8 -*-

## @package dbus_vrm
# This code takes care of the D-Bus interface (not all of below is implemented yet):
# - on startup it scans the dbus for services we know. For each known service found, it searches for
#   objects/paths we know. Everything we find is stored in items{}, and an event is registered: if a
#   value changes weĺl be notified and can pass that on to our owner. For example the vrmLogger.
#   we know.
# - after startup, it continues to monitor the dbus:
#		1) when services are added we do the same check on that
#		2) when services are removed, we remove any items that we had that referred to that service
#		3) if an existing services adds paths we update ourselves as well: on init, we make a
#		   VeDbusItemImport for a non-, or not yet existing objectpaths as well1
#
# Code is used by the vrmLogger, and also the pubsub code. Both are other modules in the dbus_vrm repo.

from dbus.mainloop.glib import DBusGMainLoop
import gobject
from gobject import idle_add
import dbus
import dbus.service
import inspect
import logging
import argparse
import pprint
import traceback
import os

# our own packages
from vedbus import VeDbusItemExport, VeDbusItemImport
from ve_utils import exit_on_error, wrap_dbus_value, unwrap_dbus_value
notfound = object() # For lookups where None is a valid result

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
class SystemBus(dbus.bus.BusConnection):
	def __new__(cls):
		return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)

class SessionBus(dbus.bus.BusConnection):
	def __new__(cls):
		return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)

class DbusMonitor(object):
	## Constructor
	def __init__(self, dbusTree, valueChangedCallback=None, deviceAddedCallback=None,
					deviceRemovedCallback=None, vebusDeviceInstance0=False):
		# valueChangedCallback is the callback that we call when something has changed.
		# def value_changed_on_dbus(dbusServiceName, dbusPath, options, changes, deviceInstance):
		# in which changes is a tuple with GetText() and GetValue()
		self.valueChangedCallback = valueChangedCallback
		self.deviceAddedCallback = deviceAddedCallback
		self.deviceRemovedCallback = deviceRemovedCallback
		self.dbusTree = dbusTree
		self.vebusDeviceInstance0 = vebusDeviceInstance0

		# Lists all tracked services. Stores name, id, device instance, value per path, and whenToLog info
		# indexed by service name (eg. com.victronenergy.settings).
		self.servicesByName = {}

		# Same values as self.servicesByName, but indexed by service id (eg. :1.30)
		self.servicesById = {}

		# For a PC, connect to the SessionBus
		# For a CCGX, connect to the SystemBus
		self.dbusConn = SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()

		# subscribe to NameOwnerChange for bus connect / disconnect events.
		(dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ \
			else dbus.SystemBus()).add_signal_receiver(
			self.dbus_name_owner_changed,
			signal_name='NameOwnerChanged')

		# Subscribe to PropertiesChanged for all services
		self.dbusConn.add_signal_receiver(self.handler_value_changes,
			dbus_interface='com.victronenergy.BusItem',
			signal_name='PropertiesChanged', path_keyword='path',
			sender_keyword='senderId')

		logger.info('===== Search on dbus for services that we will monitor starting... =====')
		serviceNames = self.dbusConn.list_names()
		for serviceName in serviceNames:
			self.scan_dbus_service(serviceName)

		logger.info('===== Search on dbus for services that we will monitor finished =====')

	def dbus_name_owner_changed(self, name, oldowner, newowner):
		if not name.startswith("com.victronenergy."):
			return

		#decouple, and process in main loop
		idle_add(exit_on_error, self._process_name_owner_changed, name, oldowner, newowner)

	def _process_name_owner_changed(self, name, oldowner, newowner):
		if newowner != '':
			# so we found some new service. Check if we can do something with it.
			newdeviceadded = self.scan_dbus_service(name)
			if newdeviceadded and self.deviceAddedCallback is not None:
				self.deviceAddedCallback(name, self.get_device_instance(name))

		elif name in self.servicesByName:
			# it disappeared, we need to remove it.
			logger.info("%s disappeared from the dbus. Removing it from our lists" % name)
			service = self.servicesByName[name]
			deviceInstance = service['deviceInstance']
			del self.servicesById[service['id']]
			del self.servicesByName[name]
			if self.deviceRemovedCallback is not None:
				self.deviceRemovedCallback(name, deviceInstance)

	def scan_dbus_service(self, serviceName):
		try:
			return self.scan_dbus_service_inner(serviceName)
		except:
			logger.error("Ignoring %s because of error while scanning:" % (serviceName))
			traceback.print_exc()
			return False

			# Errors 'org.freedesktop.DBus.Error.ServiceUnknown' and
			# 'org.freedesktop.DBus.Error.Disconnected' seem to happen when the service
			# disappears while its being scanned. Which might happen, but is not really
			# normal either, so letting them go into the logs.

	# Scans the given dbus service to see if it contains anything interesting for us. If it does, add
	# it to our list of monitored D-Bus services.
	def scan_dbus_service_inner(self, serviceName):

		# make it a normal string instead of dbus string
		serviceName = str(serviceName)

		paths = self.dbusTree.get('.'.join(serviceName.split('.')[0:3]), None)
		if paths is None:
			logger.debug("Ignoring service %s, not in the tree" % serviceName)
			return False

		logger.info("Found: %s, scanning and storing items" % serviceName)

		# we should never be notified to add a D-Bus service that we already have. If this assertion
		# raises, check process_name_owner_changed, and D-Bus workings.
		assert serviceName not in self.servicesByName

		service = {'name': serviceName, 'paths': {}, 'seen': {}}

		# create the empty list items.
		whentologoptions = ['configChange', 'onIntervalAlwaysAndOnEvent', 'onIntervalOnlyWhenChanged',
						'onIntervalAlways']

		# these lists will contain the VeDbusItemImport objects with that whenToLog setting. Used to
		for whentolog in whentologoptions:
			service[whentolog] = []

		serviceId = self.dbusConn.get_name_owner(serviceName)
		service['id'] = serviceId

		assert serviceId not in self.servicesById

		# for vebus.ttyO1, this is workaround, since VRM Portal expects the main vebus
		# devices at instance 0. Not sure how to fix this yet.
		if serviceName == 'com.victronenergy.vebus.ttyO1' and self.vebusDeviceInstance0:
			di = 0
		elif serviceName == 'com.victronenergy.settings':
			di = 0
		elif serviceName.startswith('com.victronenergy.vecan.'):
			di = 0
		else:
			try:
				di = self.dbusConn.call_blocking(serviceName,
					'/DeviceInstance', None, 'GetValue', '', [])
			except dbus.exceptions.DBusException:
				logger.info("       %s was skipped because it has no device instance" % serviceName)
				return False # Skip it
			else:
				di = int(di)

		service['deviceInstance'] = di

		logger.info("       %s has device instance %s" % (serviceName, di))

		# Let's try to fetch everything in one go
		values = {}
		texts = {}
		try:
			values.update(self.dbusConn.call_blocking(serviceName, '/', None, 'GetValue', '', []))
			texts.update(self.dbusConn.call_blocking(serviceName, '/', None, 'GetText', '', []))
		except:
			pass

		for path, options in paths.iteritems():
			# path will be the D-Bus path: '/Ac/ActiveIn/L1/V'
			# options will be a dictionary: {'code': 'V', 'whenToLog': 'onIntervalAlways'}
			# check that the whenToLog setting is set to something we expect
			assert options['whenToLog'] is None or options['whenToLog'] in whentologoptions

			# Try to obtain the value we want from our bulk fetch. If we
			# cannot find it there, do an individual query.
			value = values.get(path[1:], notfound)
			if value != notfound:
				service['seen'][path] = True
			text = texts.get(path[1:], notfound)
			if value is notfound or text is notfound:
				try:
					value = self.dbusConn.call_blocking(serviceName, path, None, 'GetValue', '', [])
					service['seen'][path] = True
					text = self.dbusConn.call_blocking(serviceName, path, None, 'GetText', '', [])
				except dbus.exceptions.DBusException as e:
					if e.get_dbus_name() in (
							'org.freedesktop.DBus.Error.ServiceUnknown',
							'org.freedesktop.DBus.Error.Disconnected'):
						raise # This exception will be handled below

					# TODO org.freedesktop.DBus.Error.UnknownMethod really
					# shouldn't happen but sometimes does.
					logger.debug("%s %s does not exist (yet)" % (serviceName, path))
					value = None
					text = None

			service['paths'][path] = [unwrap_dbus_value(value), unwrap_dbus_value(text), options]

			if options['whenToLog']:
				service[options['whenToLog']].append(path)


		logger.debug("Finished scanning and storing items for %s" % serviceName)

		# Adjust self at the end of the scan, so we don't have an incomplete set of
		# data if an exception occurs during the scan.
		self.servicesByName[serviceName] = service
		self.servicesById[serviceId] = service

		return True

	def handler_value_changes(self, changes, path, senderId):
		try:
			service = self.servicesById[senderId]
			a = service['paths'][path]
		except KeyError:
			# Either senderId or path isn't there, which means
			# it hasn't been scanned yet.
			return

		# If this properyChange does not involve a value, our work is done.
		if 'Value' not in changes:
			return

		# First update our store to the new value
		changes['Value'] = unwrap_dbus_value(changes['Value'])
		if a[0] == changes['Value']:
			return

		a[0] = changes['Value']
		try:
			a[1] = changes['Text']
		except KeyError:
			# Some services don't send Text with their PropertiesChanged events.
			a[1] = str(a[0])

		service['seen'][path] = True

		# And do the rest of the processing in on the mainloop
		if self.valueChangedCallback is not None:
			idle_add(exit_on_error, self._execute_value_changes, service['name'], path, changes, a[2])

	def _execute_value_changes(self, serviceName, objectPath, changes, options):
		# double check that the service still exists, as it might have
		# disappeared between scheduling-for and executing this function.
		if serviceName not in self.servicesByName:
			return

		self.valueChangedCallback(serviceName, objectPath,
			options, changes, self.get_device_instance(serviceName))

	# Gets the value for a certain servicename and path
	# returns the default_value when either the requested service and objectPath combination does not exist,
	# or when its value is INVALID
	def get_value(self, serviceName, objectPath, default_value=None):
		service = self.servicesByName.get(serviceName, None)
		if service is None:
			return default_value

		value = service['paths'].get(objectPath, None)
		if value is None:
			return default_value

		return value[0]

	# returns if a dbus exists now, by doing a blocking dbus call.
	# Typically seen will be sufficient and doesn't need access to the dbus.
	def exists(self, serviceName, objectPath):
		try:
			self.dbusConn.call_blocking(serviceName, objectPath, None, 'GetValue', '', [])
			return True
		except dbus.exceptions.DBusException as e:
			return False

	# Returns if there ever was a successful GetValue or valueChanged event.
	# Unlike get_value this return True also if the actual value is invalid.
	#
	# Note: the path might no longer exists anymore, but that doesn't happen in
	# practice. If a service really wants to reconfigure itself typically it should
	# reconnect to the dbus which causes it to be rescanned and seen will be updated.
	# If it is really needed to know if a path still exists, use exists.
	def seen(self, serviceName, objectPath):
		try:
			return self.servicesByName[serviceName]['seen'][objectPath]
		except KeyError:
			return False

	# Sets the value for a certain servicename and path, returns the return value of the D-Bus SetValue
	# method. If the underlying item does not exist (the service does not exist, or the objectPath was not
	# registered) the function will return -1
	def set_value(self, serviceName, objectPath, value):
		# Check if the D-Bus object referenced by serviceName and objectPath is registered. There is no
		# necessity to do this, but it is in line with previous implementations which kept VeDbusItemImport
		# objects for registers items only.
		service = self.servicesByName.get(serviceName, None)
		if service is None:
			return -1
		if objectPath not in service['paths']:
			return -1
		# We do not catch D-Bus exceptions here, because the previous implementation did not do that either.
		return self.dbusConn.call_blocking(serviceName, objectPath,
				   dbus_interface='com.victronenergy.BusItem',
				   method='SetValue', signature=None,
				   args=[wrap_dbus_value(value)])

	# Similar to set_value, but operates asynchronously
	def set_value_async(self, serviceName, objectPath, value,
			reply_handler=None, error_handler=None):
		service = self.servicesByName.get(serviceName, None)
		if service is not None:
			if objectPath in service['paths']:
				self.dbusConn.call_async(serviceName, objectPath,
					dbus_interface='com.victronenergy.BusItem',
					method='SetValue', signature=None,
					args=[wrap_dbus_value(value)],
					reply_handler=reply_handler, error_handler=error_handler)
				return

		if error_handler is not None:
			error_handler(TypeError('Service or path not found, '
						'service=%s, path=%s' % (serviceName, objectPath)))

	# returns a dictionary, keys are the servicenames, value the instances
	# optionally use the classfilter to get only a certain type of services, for
	# example com.victronenergy.battery.
	def get_service_list(self, classfilter=None):
		r = {}
		if classfilter is not None:
			class_as_list = classfilter.split('.')[0:3]

		for servicename in self.servicesByName:
			if classfilter is None or servicename.split('.')[0:3] == class_as_list:
				r[servicename] = self.get_device_instance(servicename)

		return r

	def get_device_instance(self, serviceName):
		return self.servicesByName[serviceName]['deviceInstance']

	# Parameter categoryfilter is to be a list, containing the categories you want (configChange,
	# onIntervalAlways, etc).
	# Returns a dictionary, keys are codes + instance, in VRM querystring format. For example vvt[0]. And
	# values are the value.
	def get_values(self, categoryfilter, converter=None):

		result = {}

		for serviceName in self.servicesByName:
			result.update(self.get_values_for_service(categoryfilter, serviceName, converter))

		return result

	# same as get_values above, but then for one service only
	def get_values_for_service(self, categoryfilter, servicename, converter=None):
		deviceInstance = self.get_device_instance(servicename)
		result = {}

		service = self.servicesByName[servicename]

		for category in categoryfilter:

			for path in service[category]:

				value, text, options = service['paths'][path]

				if value is not None:

					value = value if converter is None else converter.convert(path, options['code'], value, text)

					precision = options.get('precision')
					if precision:
						value = round(value, precision)

					result[options['code'] + "[" + str(deviceInstance) + "]"] = value

		return result


# ====== ALL CODE BELOW THIS LINE IS PURELY FOR DEVELOPING THIS CLASS ======

# Example function that can be used as a starting point to use this code
def value_changed_on_dbus(dbusServiceName, dbusPath, dict, changes, deviceInstance):
	logger.debug("0 ----------------")
	logger.debug("1 %s%s changed" % (dbusServiceName, dbusPath))
	logger.debug("2 vrm dict     : %s" % dict)
	logger.debug("3 changes-text: %s" % changes['Text'])
	logger.debug("4 changes-value: %s" % changes['Value'])
	logger.debug("5 deviceInstance: %s" % deviceInstance)
	logger.debug("6 - end")


def nameownerchange(a, b):
	# used to find memory leaks in dbusmonitor and VeDbusItemImport
	import gc
	gc.collect()
	objects = gc.get_objects()
	print len([o for o in objects if type(o).__name__ == 'VeDbusItemImport'])
	print len([o for o in objects if type(o).__name__ == 'SignalMatch'])
	print len(objects)


# We have a mainloop, but that is just for developing this code. Normally above class & code is used from
# some other class, such as vrmLogger or the pubsub Implementation.
def main():
	# Init logging
	logging.basicConfig(level=logging.DEBUG)
	logger.info(__file__ + " is starting up")

	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)

	import os
	import sys
	sys.path.insert(1, os.path.join(os.path.dirname(__file__), '../../'))

	import datalist   # from the dbus_vrm repository
	d = DbusMonitor(datalist.vrmtree, value_changed_on_dbus,
		deviceAddedCallback=nameownerchange, deviceRemovedCallback=nameownerchange)

	logger.info("==configchange values==")
	logger.info(pprint.pformat(d.get_values(['configChange'])))

	logger.info("==onIntervalAlways and onIntervalOnlyWhenChanged==")
	logger.info(pprint.pformat(d.get_values(['onIntervalAlways', 'onIntervalAlwaysAndOnEvent'])))

	# Start and run the mainloop
	logger.info("Starting mainloop, responding on only events")
	mainloop = gobject.MainLoop()
	mainloop.run()

if __name__ == "__main__":
	main()
