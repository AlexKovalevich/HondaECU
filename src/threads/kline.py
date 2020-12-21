import time
import os
import wx
from threading import Thread
from ftd2xx import FTD2XX as ftd
from pydispatch import dispatcher
# from pylibftdi import Driver, FtdiError, LibraryMissingError
import numpy as np

from eculib import KlineAdapter
from eculib.honda import *

class KlineWorker(Thread):

	def __init__(self, parent):
		self.parent = parent
		self.__clear_data()
		dispatcher.connect(self.DeviceHandler, signal="FTDIDevice", sender=dispatcher.Any)
		dispatcher.connect(self.ErrorPanelHandler, signal="ErrorPanel", sender=dispatcher.Any)
		dispatcher.connect(self.DatalogPanelHandler, signal="DatalogPanel", sender=dispatcher.Any)
		dispatcher.connect(self.ReadPanelHandler, signal="ReadPanel", sender=dispatcher.Any)
		dispatcher.connect(self.WritePanelHandler, signal="WritePanel", sender=dispatcher.Any)
		dispatcher.connect(self.HRCSettingsPanelHandler, signal="HRCSettingsPanel", sender=dispatcher.Any)
		Thread.__init__(self)

	def __cleanup(self):
		if self.ecu:
			self.ecu.dev.close()
			del self.ecu
		self.__clear_data()

	def __clear_data(self):
		self.ecu = None
		self.ready = False
		self.sendpassword = False
		self.sendwriteinit = False
		self.sendrecoverinint = False
		self.hrcmode = None
		self.senderase = False
		self.readinfo = None
		self.writeinfo = None
		self.state = ECUSTATE.UNDEFINED
		self.reset_state()

	def reset_state(self):
		self.ecmid = bytearray()
		self.errorcodes = {}
		self.update_errors = False
		self.clear_codes = False
		self.flashcount = -1
		self.dtccount = -1
		self.update_tables = False
		self.tables = None
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="ecmid", value=bytes(self.ecmid))
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="flashcount", value=self.flashcount)

	def HRCSettingsPanelHandler(self, mode, data):
		self.hrcmode = (mode, data)

	def WritePanelHandler(self, data, offset):
		self.writeinfo = [data,offset,None]

	def ReadPanelHandler(self, data, offset):
		if self.state != ECUSTATE.READ:
			self.sendpassword = True
		self.readinfo = [data,offset,None]

	def DatalogPanelHandler(self, action):
		if action == "data.on":
			self.update_tables = True
		elif action == "data.off":
			self.update_tables = False

	def ErrorPanelHandler(self, action):
		if action == "dtc.clear":
			self.clear_codes = True
		elif action == "dtc.on":
			self.update_errors = True
		elif action == "dtc.off":
			self.update_errors = False

	def DeviceHandler(self, action, vendor, product, serial):
		if action == "interrupt":
			raise Exception()
		elif action == "deactivate":
			if self.ecu:
				self.__cleanup()
		elif action == "activate":
			self.__clear_data()
			try:
				self.ecu = HondaECU(KlineAdapter(device_id=serial))
				self.ready = True
			except FtdiError:
				pass

	def read_flash(self):
		readsize = 128
		location = self.readinfo[1]
		binfile = self.readinfo[0]
		status = "bad"
		with open(binfile, "wb") as fbin:
			t = time.time()
			size = location
			rate = 0
			while not self.readinfo is None:
				info = self.ecu.send_command([0x82, 0x82, 0x00], format_read(location) + [readsize])
				if not info:
					readsize -= 1
					if readsize < 1:
						break
				else:
					fbin.write(info[2])
					fbin.flush()
					location += readsize
					n = time.time()
					wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read.progress", value=(-1,"%.02fKB @ %s" % (location/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
					if n-t > 1:
						rate = (location-size)/(n-t)
						t = n
						size = location
			if self.ecu.dev.kline():
				wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read.progress", value=(-1,"%.02fKB @ %s" % (location/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
			else:
				return "interrupted"
		with open(binfile, "rb") as fbin:
			nbyts = os.path.getsize(binfile)
			if nbyts > 0:
				byts = bytearray(fbin.read(nbyts))
				_, status, _ = do_validation(byts, nbyts)
			return status

	def write_flash(self, byts, offset=0):
		ret = 1
		writesize = 128
		ossize = len(byts)
		maxi = int(ossize/writesize)
		offseti = int(offset/16)
		i = 0
		w = 0
		t = time.time()
		rate = 0
		size = 0
		done = False
		while not self.writeinfo is None and i < maxi and not done:
			w = (i*writesize)
			bytstart = [s for s in struct.pack(">H",offseti+(8*i))]
			if i+1 == maxi:
				bytend = [s for s in struct.pack(">H",0)]
			else:
				bytend = [s for s in struct.pack(">H",offseti+(8*(i+1)))]
			d = list(byts[((i+0)*writesize):((i+1)*writesize)])
			x = bytstart + d + bytend
			c1 = checksum8bit(x)
			c2 = checksum8bitHonda(x)
			x = [0x01, 0x06] + x + [c1, c2]
			info = self.ecu.send_command([0x7e], x)
			if not info or ord(info[1]) != 5:
				wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write.progress", value=(0, "interrupted"))
				return False
			if info[2][1] == 0:
				done = True
			n = time.time()
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write.progress", value=(i/maxi*100,"%.02fKB of %.02fKB @ %s" % (w/1024.0, ossize/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
			if n-t > 1:
				rate = (w-size)/(n-t)
				t = n
				size = w
			i += 1
		info = self.ecu.send_command([0x7e], [0x01, 0x08])
		if not info is None and info[2][1] == 0x0:
			ret = 0
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(i/maxi*100,"%.02fKB of %.02fKB @ %s" % ((w-offset)/1024.0, ossize/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
		return ret

	def do_init_write(self, recover=False):
		if recover:
			self.state = ECUSTATE.INIT_RECOVER
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
			self.ecu.do_init_recover()
			self.ecu.send_command([0x72],[0x00, 0xf1])
			time.sleep(1)
			self.ecu.send_command([0x27],[0x00, 0x01, 0x00])
		else:
			self.state = ECUSTATE.INIT_WRITE
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
			self.ecu.do_init_write()
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="erase", value=None)
		for i in range(14):
			w = 14-i
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write.progress", value=(w/14*100, "waiting for %d seconds" % (w)))
			time.sleep(1)
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write.progress", value=(0, "waiting for %d seconds" % (w)))

	def do_erase(self):
		ret = 1
		self.state = ECUSTATE.ERASING
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
		self.ecu.do_erase()
		e = 0
		while True:
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write.progress", value=(np.clip(e/35*100,0,100), "erasing ecu"))
			time.sleep(.1)
			e += 1
			info = self.ecu.send_command([0x7e], [0x01, 0x05],retries=10)
			if info:
				if info[2][1] == 0x00:
					self.ecu.send_command([0x7e], [0x01, 0x01, 0x00])
					ret = 0
					break
			else:
				break
		return ret

	def do_write(self):
		ret = 1
		self.state = ECUSTATE.WRITING
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write", value=None)
		if self.write_flash(self.writeinfo[0], offset=self.writeinfo[1]) == 0:
			 self.writeinfo[2] = "good" if self.ecu.do_post_write() else "bad"
			 wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write.result", value=self.writeinfo[2])
			 ret = 0
		return ret

	def do_read(self):
		ret = 1
		self.state = ECUSTATE.READING
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read", value=None)
		self.readinfo[2] = self.read_flash()
		if self.readinfo[2] == "interrupted":
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read.progress", value=(0, "interrupted"))
		else:
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read.result", value=self.readinfo[2])
			ret = 0
		return ret

	def do_get_ecmid(self):
		ret = 1
		info = self.ecu.send_command([0x72], [0x71, 0x00])
		if info:
			self.ecmid = info[2][2:7]
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="ecmid", value=bytes(self.ecmid))
			ret = 0
		return ret

	def do_get_flashcount(self):
		ret = 1
		info = self.ecu.send_command([0x7d], [0x01, 0x01, 0x03])
		if info:
			self.flashcount = int(info[2][4])
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="flashcount", value=self.flashcount)
			ret = 0
		return ret

	def do_clear_codes(self):
		ret = 1
		while self.clear_codes:
			info = self.ecu.send_command([0x72],[0x60, 0x03])
			if info:
				if info[2][1] == 0x00:
					ret = 0
					self.dtccount = -1
					self.errorcodes = {}
					self.clear_codes = False
			else:
				self.dtccount = -1
				self.errorcodes = {}
				self.clear_codes = False
		return ret

	def do_get_dtcs(self):
		errorcodes = {}
		for type in [0x74,0x73]:
			errorcodes[hex(type)] = []
			for i in range(1,0x0c):
				info = self.ecu.send_command([0x72],[type, i])
				if info:
					for j in [3,5,7]:
						if info[2][j] != 0:
							errorcodes[hex(type)].append("%02d-%02d" % (info[2][j],info[2][j+1]))
					if info[2] == 0:
						return 1
				else:
					return 1
		dtccount = sum([len(c) for c in errorcodes.values()])
		if self.dtccount != dtccount:
			self.dtccount = dtccount
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="dtccount", value=self.dtccount)
		if self.errorcodes != errorcodes:
			self.errorcodes = errorcodes
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="dtc", value=self.errorcodes)
		return 0

	def do_probe_tables(self):
		tables = self.ecu.probe_tables()
		if len(tables) > 0:
			self.tables = tables
			tables = " ".join([hex(x) for x in self.tables.keys()])
			for t, d in self.tables.items():
				wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="data", value=(t,d[0],d[1]))
			return 0
		else:
			return 1

	def do_update_tables(self):
		for t in self.tables:
			info = self.ecu.send_command([0x72], [0x71, t])
			if info:
				if info[3] > 2:
					self.tables[t] = [info[3],info[2]]
					wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="data", value=(t,info[3],info[2]))
				else:
					return 1
			else:
				return 1
		return 0

	def do_idle_tasks(self):
		ret = 0
		if not self.ecmid:
			ret += self.do_get_ecmid()
		if self.flashcount < 0:
			ret += self.do_get_flashcount()
		if self.clear_codes:
			ret += self.do_clear_codes()
		if self.update_errors or self.dtccount < 0:
			ret += self.do_get_dtcs()
		if not self.tables:
			ret += self.do_probe_tables()
		elif self.update_tables:
			ret += self.do_update_tables()
		return ret

	def do_update_state(self):
		state = self.ecu.detect_ecu_state()
		if state != self.state:
			self.state = state
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
			if self.state == ECUSTATE.OFF:
				self.reset_state()

	def do_password(self):
		p1 = self.ecu.send_command([0x27],[0xe0, 0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x48, 0x6f])
		p2 = self.ecu.send_command([0x27],[0xe0, 0x77, 0x41, 0x72, 0x65, 0x59, 0x6f, 0x75])
		passok = (p1 != None) and (p2 != None)
		wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="password", value=passok)

	def write_helper(self, init=False, recover=False):
		ret = 1
		if self.ecu.diag():
			if init:
				self.do_init_write(recover=recover)
			self.do_erase()
			self.do_write()
			self.writeinfo = None
			ret = 0
		return ret

	def read_helper(self):
		ret = self.do_read()
		self.readinfo = None
		return ret

	def do_on_power(self):
		if self.sendpassword:
			self.do_password()
			# self.state = ECUSTATE.READ
			# wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
			self.sendpassword = False

	def do_exceptions(self):
		ret = 1
		if self.state == ECUSTATE.READ:
			if self.readinfo is not None:
				ret = self.read_helper()
		return ret

	def run(self):
		while self.parent.run:
			if not self.ready:
				time.sleep(.001)
			else:
				try:
					if self.state in [ECUSTATE.UNDEFINED, ECUSTATE.OFF, ECUSTATE.UNKNOWN]:
						self.do_update_state()
						if self.state == ECUSTATE.OK:
							self.do_on_power()
					else:
						if self.ecu.ping():
							if self.do_connected() > 0:
								self.do_update_state()
						else:
							if self.do_exceptions() > 0:
								self.do_update_state()
				except FtdiError:
					pass
				except AttributeError:
					pass
				except OSError:
					pass

	def do_connected(self):
		if self.state == ECUSTATE.OK:
			if self.writeinfo is not None:
				return self.write_helper(init=True)
			else:
				return self.do_idle_tasks()
		elif self.state == ECUSTATE.WRITE:
			if self.writeinfo is not None:
				self.write_helper(init=False)
		elif self.state == ECUSTATE.RECOVER_OLD:
			if self.writeinfo is not None:
				return self.write_helper(init=True)
		elif self.state == ECUSTATE.RECOVER_NEW:
			if self.writeinfo is not None:
				return self.write_helper(init=True, recover=True)
		return False

	# def run(self):
	# 	while self.parent.run:
	# 		if not self.ready:
	# 			time.sleep(.001)
	# 		else:
	# 			try:
	# 				if self.ecu.dev.kline():
	# 					if self.state in [ECUSTATE.OFF, ECUSTATE.UNKNOWN]:
	# 						if self.hrcmode is not None:
	# 							time.sleep(.1)
	# 							self.ecu.dev.baudrate = 19200
	# 							self.do_hrc_settings()
	# 							self.hrcmode = None
	# 							continue
	# 						time.sleep(.5)
	# 						self.ecu.dev.baudrate = 10400
	# 						self.do_update_state()
	# 						if self.readinfo is not None:
	# 							self.do_password()
	# 							self.state = ECUSTATE.READ
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
	# 						continue
	# 					if self.state == ECUSTATE.OK:
	# 						print(self.ecu.diag())
	# 						if self.ecu.diag():
	# 							if self.writeinfo is not None:
	# 								self.write_helper(init=True)
	# 								continue
	# 							self.do_idle_tasks()
	# 							continue
	# 						else:
	# 							self.do_update_state()
	# 							continue
	# 					if self.state == ECUSTATE.RECOVER_OLD:
	# 						if self.ecu.diag():
	# 							if self.writeinfo is not None:
	# 								self.write_helper(init=True)
	# 								continue
	# 						else:
	# 							self.do_update_state()
	# 							continue
	# 					if self.state == ECUSTATE.RECOVER_NEW:
	# 						if self.ecu.diag():
	# 							if self.writeinfo is not None:
	# 								self.write_helper(init=True, recover=True)
	# 								continue
	# 						else:
	# 							self.do_update_state()
	# 							continue
	# 					if self.state == ECUSTATE.WRITE:
	# 						if self.writeinfo is not None:
	# 							self.write_helper(init=False)
	# 							continue
	# 					if self.state == ECUSTATE.READ:
	# 						if self.readinfo is not None:
	# 							self.do_read()
	# 							self.readinfo = None
	# 							continue
	# 				else:
	# 					self.do_update_state()
	# 				time.sleep(.1)
	# 			except FtdiError:
	# 				pass
	# 			except AttributeError:
	# 				pass
	# 			except OSError:
	# 				pass

	# def run(self):
	# 	while self.parent.run:
	# 		if not self.ready:
	# 			time.sleep(.001)
	# 		else:
	# 			try:
	# 				if self.hrcmode != None:
	# 					if self.ecu.dev.baudrate == 10400:
	# 						while self.ecu.dev.kline():
	# 							time.sleep(.1)
	# 						self.state = ECUSTATE.OFF
	# 						wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=self.state)
	# 						self.ecu.dev.baudrate = 19200
	# 					elif self.ecu.dev.baudrate == 19200:
	# 						if self.ecu.dev.kline():
	# 							for i in range(4):
	# 								wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=((4-i)/4*100, "waiting"))
	# 								time.sleep(1)
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=(0, "waiting"))
	# 							self.ecu.send_command([0x4d,0x4b],[0x0f, 0x00], retries=0)
	# 							hrcid = self.ecu.send_command([0x4d,0x4b],[0x00, 0x00], retries=0)[2][2:]
	# 							self.ecu.send_command([0x4d,0x4b],[0x0e, 0x00], retries=0)
	# 							rpm = self.ecu.send_command([0x4d,0x4b],[0x04, 0x01], retries=0)[2][2:] # RPM Breakpoints
	# 							throttle = self.ecu.send_command([0x4d,0x4b],[0x05, 0x01], retries=0)[2][2:] # Throttle Breakpoints
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=(1/5*100, ""))
	# 							fimap2 = self.ecu.send_command([0x4d,0x4b],[0x13, 0x01], retries=0)[2][2:] # FIMAP Mode2
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=(2/5*100, ""))
	# 							fimap3 = self.ecu.send_command([0x4d,0x4b],[0x14, 0x01], retries=0)[2][2:] # FIMAP Mode3
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=(3/5*100, ""))
	# 							igmap2 = self.ecu.send_command([0x4d,0x4b],[0x24, 0x01], retries=0)[2][2:] # IGMAP Mode2
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=(4/5*100, ""))
	# 							igmap3 = self.ecu.send_command([0x4d,0x4b],[0x25, 0x01], retries=0)[2][2:] # IGMAP Mode3
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.progress", value=(5/5*100, ""))
	# 							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hrc.read.result", value=True)
	# 							with open(self.hrcmode[1][0], "wb") as f:
	# 								comment = b"HondaECU"
	# 								name = self.hrcmode[1][1].encode("ascii")
	# 								d = b"\x16" + \
	# 									b"FiSettingTool_UserData" + \
	# 									bytes(hrcid) + \
	# 									b"CRF450R" + \
	# 									b"\x09" + \
	# 									b"PGM-FI/IG" + \
	# 									bytes([len(rpm)]) + \
	# 									bytes(rpm) + \
	# 									bytes([len(throttle)]) + \
	# 									bytes(throttle) + \
	# 									bytes([len(fimap2)]) + \
	# 									bytes(fimap2) + \
	# 									bytes(fimap3) + \
	# 									bytes([len(igmap2)]) + \
	# 									bytes(igmap2) + \
	# 									bytes(igmap3) + \
	# 									b"\x00" * 192 + \
	# 									b"\x0c" + \
	# 									b"\x00" * 22 + \
	# 									bytes([len(name)]) + \
	# 									bytes(name) + \
	# 									bytes([len(comment)]) + \
	# 									comment
	# 								f.write(d)
	# 							self.hrcmode = None
	# 							self.do_unknown()
	# 					continue
