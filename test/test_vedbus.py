#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Python
import logging
import os
import gobject
import sqlite3
import sys
import unittest
import subprocess
import time
import platform
import dbus
import threading
import fcntl
from dbus.mainloop.glib import DBusGMainLoop

# Local
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '../'))
from vedbus import VeDbusService, VeDbusItemImport

logger = logging.getLogger(__file__)
"""
class VeDbusServiceTests(unittest.TestCase):
	def incrementcallback(self, path, value):
		self.calledback += 1
		return True if value < 50 else False

	def setUp(self):
		self.calledback = 0


		self.service = VeDbusService('com.victronenergy.testservice')
		self.service.add_path(path='/Int', value=10, description="int", writeable=True,
			onchangecallback=self.incrementcallback, gettextcallback=None)

		self.thread = threading.Thread(target=self.mainloop.run)
		self.thread.start()

	def test_callback(self):
		a = subprocess.check_output('dbus', '-y com.victronenergy.testservice')
		print(a)

	def tearDown(self):
		self.thread.kill()
		self.thread = None
"""


class VeDbusItemExportTests(unittest.TestCase):
	# The actual code calling VeDbusItemExport is in fixture_vedbus.py, which is ran as a subprocess. That
	# code exports several values to the dbus. And then below test cases check if the exported values are
	# what the should be, by using the bare dbus import objects and functions.

	def setUp(self):
		self.sp = subprocess.Popen([sys.executable, "fixture_vedbus.py"], stdout=subprocess.PIPE)
		self.dbusConn = dbus.SystemBus() if (platform.machine() == 'armv7l') else dbus.SessionBus()

		#wait for fixture to be up and running
		while (self.sp.stdout.readline().rstrip() != 'up and running'):
			pass

	def tearDown(self):
		self.sp.kill()
		self.sp.wait()

	def test_get_value_invalid(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/Invalid').GetValue()
		self.assertEqual(v, dbus.Array([], signature=dbus.Signature('i'), variant_level=1))
		self.assertIs(type(v), dbus.Array)
		self.assertEqual(self.dbusConn.get_object('com.victronenergy.dbusexample', '/Invalid').GetText(), '---')

	def test_get_value_string(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/String').GetValue()
		self.assertEqual(v, 'this is a string')
		self.assertIs(type(v), dbus.String)
		self.assertEqual(self.dbusConn.get_object('com.victronenergy.dbusexample', '/String').GetText(), 'this is a string')

	def test_get_value_int(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/Int').GetValue()
		self.assertEqual(v, 40000)
		self.assertIs(type(v), dbus.Int32)
		self.assertEqual(self.dbusConn.get_object('com.victronenergy.dbusexample', '/Int').GetText(), '40000')

	def test_get_value_negativeint(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/NegativeInt').GetValue()
		self.assertEqual(v, -10)
		self.assertIs(type(v), dbus.Int32)
		self.assertEqual(self.dbusConn.get_object('com.victronenergy.dbusexample', '/NegativeInt').GetText(), '-10')

	def test_get_value_float(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/Float').GetValue()
		self.assertEqual(v, 1.5)
		self.assertIs(type(v), dbus.Double)
		self.assertEqual(self.dbusConn.get_object('com.victronenergy.dbusexample', '/Float').GetText(), '1.5')

	def test_get_text_byte(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/Byte').GetText()
		self.assertEqual('84', v)

	def test_get_value_byte(self):
		v = self.dbusConn.get_object('com.victronenergy.dbusexample', '/Byte').GetValue()
		self.assertEqual(84, v)

	def test_set_value(self):
		self.assertNotEqual(0, self.dbusConn.get_object('com.victronenergy.dbusexample', '/NotWriteable').SetValue(12))
		self.assertEqual('original', self.dbusConn.get_object('com.victronenergy.dbusexample', '/NotWriteable').GetValue())

		self.assertEqual(0, self.dbusConn.get_object('com.victronenergy.dbusexample', '/Writeable').SetValue(12))
		self.assertEqual(12, self.dbusConn.get_object('com.victronenergy.dbusexample', '/Writeable').GetValue())

		self.assertNotEqual(0, self.dbusConn.get_object('com.victronenergy.dbusexample', '/WriteableUpTo100').SetValue(102))
		self.assertEqual('original', self.dbusConn.get_object('com.victronenergy.dbusexample', '/WriteableUpTo100').GetValue())

		self.assertEqual(0, self.dbusConn.get_object('com.victronenergy.dbusexample', '/WriteableUpTo100').SetValue(50))
		self.assertEqual(50, self.dbusConn.get_object('com.victronenergy.dbusexample', '/WriteableUpTo100').GetValue())

	def test_gettextcallback(self):
		self.assertEqual('gettexted /Gettextcallback 10', self.dbusConn.get_object('com.victronenergy.dbusexample', '/Gettextcallback').GetText())

	def waitandkill(self, seconds=5):
		time.sleep(seconds)
		self.process.kill()
		self.process.wait()

	def test_changedsignal(self):
		self.process = subprocess.Popen(['dbus-monitor', "type='signal',sender='com.victronenergy.dbusexample',interface='com.victronenergy.BusItem'"], stdout=subprocess.PIPE)

		#wait for dbus-monitor to start up
		time.sleep(0.5)

		#set timeout
		thread = threading.Thread(target=self.waitandkill)
		thread.start()

		self.dbusConn.get_object('com.victronenergy.dbusexample', '/Gettextcallback').SetValue(60)

		fcntl.fcntl(self.process.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)

		time.sleep(0.5)

		t = ""
		while True:
			try:
				t += self.process.stdout.readline()
			except IOError:
				break

		a = "-> dest=(null destination) serial=4 path=/Gettextcallback; interface=com.victronenergy.BusItem; member=PropertiesChanged\n"
		a += "   array [\n"
		a += "      dict entry(\n"
		a += "         string \"Text\"\n"
		a += "         variant             string \"gettexted /Gettextcallback 60\"\n"
		a += "      )\n"
		a += "      dict entry(\n"
		a += "         string \"Value\"\n"
		a += "         variant             int32 60\n"
		a += "      )\n"
		a += "   ]"

		self.assertNotEqual(-1, t.find(a))

		thread.join()

"""
MVA 2014-08-30: this test of VEDbusItemImport doesn't work, since there is no gobject-mainloop.
Probably making some automated functional test, using bash and some scripts, will work much
simpler and better
class VeDbusItemImportTests(unittest.TestCase):
	# VeDbusItemImport class is tested against dbus objects exported by fixture_vedbus.py, which is ran as a
	# subprocess.

	def setUp(self):
		self.sp = subprocess.Popen([sys.executable, "fixture_vedbus.py"], stdout=subprocess.PIPE)
		self.dbusConn = dbus.SystemBus() if (platform.machine() == 'armv7l') else dbus.SessionBus()

		#wait for fixture to be up and running
		while (self.sp.stdout.readline().rstrip() != 'up and running'):
			pass

	def tearDown(self):
		self.sp.kill()
		self.sp.wait()

	def test_get_invalid(self):
		self.assertIs(None, VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/Invalid').get_value())
		self.assertEqual('---', VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/Invalid').get_text())

	def test_get_string(self):
		v = VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/String')
		self.assertEqual('this is a string', v.get_value())
		self.assertIs(dbus.String, type(v.get_value()))
		self.assertEqual('this is a string', v.get_text())

	def test_get_int(self):
		v = VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/Int')
		self.assertEqual(40000, v.get_value())
		self.assertIs(dbus.Int32, type(v.get_value()))
		self.assertEqual('40000', v.get_text())

	def test_get_byte(self):
		v = VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/Byte')
		self.assertEqual(84, v.get_value())
		self.assertEqual('84', v.get_text())

	def test_set_value(self):
		nw = VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/NotWriteable')
		wr = VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/Writeable')
		wc = VeDbusItemImport(self.dbusConn, 'com.victronenergy.dbusexample', '/WriteableUpTo100')

		self.assertNotEqual(0, nw.set_value(12))
		self.assertEqual('original', nw.get_value())

		self.assertEqual(0, wr.set_value(12))
		self.assertEqual(12, wr.get_value())

		self.assertNotEqual(0, wc.set_value(102))
		self.assertEqual('original', wc.get_value())

		self.assertEqual(0, wc.set_value(50))
		self.assertEqual(50, wc.get_value())
"""

if __name__ == "__main__":
	logging.basicConfig(stream=sys.stderr)
	logging.getLogger('').setLevel(logging.WARNING)
	unittest.main()
