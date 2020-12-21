import sys
import os
sys.path.insert(0, os.path.abspath('/mnt/data/workspace/eculib'))

import json
import time

from pydispatch import dispatcher

import wx
import wx.lib.buttons as buttons

from frames.info import HondaECU_InfoPanel
from frames.data import HondaECU_DatalogPanel
from frames.error import HondaECU_ErrorPanel
from frames.flash import HondaECU_FlashPanel
from frames.hrcsettings import HondaECU_HRCDataSettingsPanel
from frames.tune import TunePanel
from frames.tunehelper import HondaECU_TunePanelHelper

from threads.kline import KlineWorker
from threads.usb import USBMonitor

import tarfile

from ecmids import ECM_IDs

from eculib.honda import ECUSTATE, checksum8bitHonda

class HondaECU_AppButton(buttons.ThemedGenBitmapTextButton):

	def __init__(self, appid, enablestates, *args, **kwargs):
		self.appid = appid
		self.enablestates = enablestates
		buttons.ThemedGenBitmapTextButton.__init__(self, *args,**kwargs)
		self.SetInitialSize((128,64))

	def DrawLabel(self, dc, width, height, dx=0, dy=0):
		bmp = self.bmpLabel
		if bmp is not None:
			if self.bmpDisabled and not self.IsEnabled():
				bmp = self.bmpDisabled
			if self.bmpFocus and self.hasFocus:
				bmp = self.bmpFocus
			if self.bmpSelected and not self.up:
				bmp = self.bmpSelected
			bw,bh = bmp.GetWidth(), bmp.GetHeight()
			if not self.up:
				dx = dy = self.labelDelta
			hasMask = bmp.GetMask() is not None
		else:
			bw = bh = 0
			hasMask = False

		dc.SetFont(self.GetFont())
		if self.IsEnabled():
			dc.SetTextForeground(self.GetForegroundColour())
		else:
			dc.SetTextForeground(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

		label = self.GetLabel()
		tw, th = dc.GetTextExtent(label)
		if not self.up:
			dx = dy = self.labelDelta

		if bmp is not None:
			dc.DrawBitmap(bmp, (width-bw)/2, (height-bh-th-4)/2, hasMask)
		dc.DrawText(label, (width-tw)/2, (height+bh-th+4)/2)

class HondaECU_LogPanel(wx.Frame):

	def __init__(self, parent):
		self.auto = True
		wx.Frame.__init__(self, parent, title="HondaECU :: Debug Log", size=(640,480))
		self.SetMinSize((640,480))

		self.menubar = wx.MenuBar()
		self.SetMenuBar(self.menubar)
		fileMenu = wx.Menu()
		self.menubar.Append(fileMenu, '&File')
		saveItem = wx.MenuItem(fileMenu, wx.ID_SAVEAS, '&Save As\tCtrl+S')
		self.Bind(wx.EVT_MENU, self.OnSave, saveItem)
		fileMenu.Append(saveItem)
		fileMenu.AppendSeparator()
		quitItem = wx.MenuItem(fileMenu, wx.ID_EXIT, '&Quit\tCtrl+Q')
		self.Bind(wx.EVT_MENU, self.OnClose, quitItem)
		fileMenu.Append(quitItem)
		viewMenu = wx.Menu()
		self.menubar.Append(viewMenu, '&View')
		self.autoscrollItem = viewMenu.AppendCheckItem(wx.ID_ANY, 'Auto scroll log')
		self.autoscrollItem.Check()
		self.logText = wx.TextCtrl(self, style = wx.TE_MULTILINE|wx.TE_READONLY|wx.HSCROLL)
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(self.logText, 1, wx.EXPAND|wx.ALL, 5)
		self.SetSizer(sizer)
		self.Bind(wx.EVT_CLOSE, self.OnClose)
		self.Layout()
		sizer.Fit(self)
		self.Center()
		self.starttime = time.time()
		wx.CallAfter(dispatcher.connect, self.ECUDebugHandler, signal="ecu.debug", sender=dispatcher.Any)

	def OnSave(self, event):
		with wx.FileDialog(self, "Save debug log", wildcard="Debug log files (*.txt)|*.txt", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fileDialog:
			if fileDialog.ShowModal() == wx.ID_CANCEL:
				return
			pathname = fileDialog.GetPath()
			try:
				with open(pathname, 'w') as file:
					file.write(self.logText.GetValue())
			except IOError:
				print("Cannot save current data in file '%s'." % pathname)

	def OnClose(self, event):
		self.Hide()

	def ECUDebugHandler(self, msg):
		msg = "[%.4f] %s\n" % (time.time()-self.starttime, msg)
		if self.autoscrollItem.IsChecked():
			wx.CallAfter(self.logText.AppendText, msg)
		else:
			wx.CallAfter(self.logText.WriteText, msg)

class HondaECU_ControlPanel(wx.Frame):

	def __init__(self, version_full, nobins=False, restrictions=None, force_restrictions=False):
		self.nobins = nobins
		self.restrictions = restrictions
		self.force_restrictions = force_restrictions
		self.run = True
		self.active_ftdi_device = None
		self.ftdi_devices = {}
		self.__clear_data()

		if getattr(sys, 'frozen', False):
			self.basepath = sys._MEIPASS
		else:
			self.basepath = os.path.dirname(os.path.realpath(__file__))

		self.version_full = version_full
		self.version_short = self.version_full.split("-")[0]

		self.apps = {
			"flash": {
				"label":"Flash",
				"icon":"images/chip2.png",
				"conflicts":["data","hrc"],
				"panel":HondaECU_FlashPanel,
				"disabled":True,
				"enable": [ECUSTATE.OK, ECUSTATE.RECOVER_OLD, ECUSTATE.RECOVER_NEW, ECUSTATE.WRITE, ECUSTATE.READ],
			},
			"tunehelper": {
				"label":"Tune",
				"icon":"images/bike.png",
				"panel":HondaECU_TunePanelHelper,
			},
			"info": {
				"label":"ECU Info",
				"icon":"images/info2.png",
				"conflicts":["flash","hrc"],
				"panel":HondaECU_InfoPanel,
			},
			"data": {
				"label":"Data Logging",
				"icon":"images/monitor.png",
				"conflicts":["flash","hrc"],
				"panel":HondaECU_DatalogPanel,
				"disabled":True,
				"enable": [ECUSTATE.OK],
			},
			"dtc": {
				"label":"Trouble Codes",
				"icon":"images/warning.png",
				"conflicts":["flash","hrc"],
				"panel":HondaECU_ErrorPanel,
				"disabled":True,
				"enable": [ECUSTATE.OK],
			},
			# "hrcsettings": {
			# 	"label":"HRC Settings",
			# 	"icon":"images/cog.png",
			# 	"conflicts":["flash","data","dtc","info"],
			# 	"panel":HondaECU_HRCDataSettingsPanel,
			# 	"disabled":True,
			# 	"enable": [ECUSTATE.OK],
			# },

		}
		self.appanels = {}

		wx.Frame.__init__(self, None, title="HondaECU %s :: Control Panel" % (self.version_short), style=wx.DEFAULT_FRAME_STYLE ^ wx.RESIZE_BORDER, size=(500,300))

		ib = wx.IconBundle()
		ib.AddIcon(os.path.join(self.basepath,"images","honda.ico"))
		self.SetIcons(ib)

		self.menubar = wx.MenuBar()
		self.SetMenuBar(self.menubar)
		fileMenu = wx.Menu()
		self.menubar.Append(fileMenu, '&File')
		self.devicesMenu = wx.Menu()
		fileMenu.AppendSubMenu(self.devicesMenu, "Devices")
		fileMenu.AppendSeparator()
		quitItem = wx.MenuItem(fileMenu, wx.ID_EXIT, '&Quit\tCtrl+Q')
		self.Bind(wx.EVT_MENU, self.OnClose, quitItem)
		fileMenu.Append(quitItem)
		helpMenu = wx.Menu()
		self.menubar.Append(helpMenu, '&Help')
		debugItem = wx.MenuItem(helpMenu, wx.ID_ANY, 'Show debug log')
		self.Bind(wx.EVT_MENU, self.OnDebug, debugItem)
		helpMenu.Append(debugItem)
		helpMenu.AppendSeparator()
		detectmapItem = wx.MenuItem(helpMenu, wx.ID_ANY, 'Detect map id')
		self.Bind(wx.EVT_MENU, self.OnDetectMap, detectmapItem)
		helpMenu.Append(detectmapItem)
		checksumItem = wx.MenuItem(helpMenu, wx.ID_ANY, 'Validate bin checksum')
		self.Bind(wx.EVT_MENU, self.OnBinChecksum, checksumItem)
		helpMenu.Append(checksumItem)

		self.statusbar = self.CreateStatusBar(1)
		self.statusbar.SetSize((-1, 28))
		self.statusbar.SetStatusStyles([wx.SB_SUNKEN])
		self.SetStatusBar(self.statusbar)

		self.outerp = wx.Panel(self)
		self.wrappanel = wx.Panel(self.outerp)
		wrapsizer = wx.WrapSizer(wx.HORIZONTAL)
		self.appbuttons = {}
		for a,d in self.apps.items():
			icon = wx.Image(os.path.join(self.basepath, d["icon"]), wx.BITMAP_TYPE_ANY).ConvertToBitmap()
			enablestates = None
			if "enable" in d:
				enablestates = d["enable"]
			self.appbuttons[a] = HondaECU_AppButton(a, enablestates, self.wrappanel, wx.ID_ANY, icon, label=d["label"])
			if "disabled" in d and d["disabled"]:
				self.appbuttons[a].Disable()
			wrapsizer.Add(self.appbuttons[a], 0)
			self.Bind(wx.EVT_BUTTON, self.OnAppButtonClicked, self.appbuttons[a])
		self.wrappanel.SetSizer(wrapsizer)

		self.outersizer = wx.BoxSizer(wx.VERTICAL)
		self.outersizer.Add(self.wrappanel, 1, wx.EXPAND)
		self.outerp.SetSizer(self.outersizer)

		self.mainsizer = wx.BoxSizer(wx.VERTICAL)
		self.mainsizer.Add(self.outerp, 1, wx.EXPAND)
		self.SetSizer(self.mainsizer)

		self.Bind(wx.EVT_CLOSE, self.OnClose)

		self.debuglog = HondaECU_LogPanel(self)

		dispatcher.connect(self.USBMonitorHandler, signal="USBMonitor", sender=dispatcher.Any)
		dispatcher.connect(self.AppPanelHandler, signal="AppPanel", sender=dispatcher.Any)
		dispatcher.connect(self.KlineWorkerHandler, signal="KlineWorker", sender=dispatcher.Any)
		dispatcher.connect(self.TunePanelHelperHandler, signal="TunePanelHelper", sender=dispatcher.Any)

		self.usbmonitor = USBMonitor(self)
		self.klineworker = KlineWorker(self)

		self.Layout()
		self.mainsizer.Fit(self)
		self.Center()
		self.Show()

		self.usbmonitor.start()
		self.klineworker.start()

	def __clear_data(self):
		self.ecuinfo = {}

	def TunePanelHelperHandler(self, xdf, bin, metainfo, htf=None):
		if htf != None:
			tar = tarfile.open(htf, "r:xz")
			xdfs = None
			binorig = None
			binmod = None
			metainfo = None
			for f in tar.getnames():
				if f == "metainfo.json":
					metainfo = json.load(tar.extractfile(f))
				else:
					b,e = os.path.splitext(f)
					if e == ".xdf":
						xdfs = tar.extractfile(f).read()
					elif e == ".bin":
						x, y = os.path.splitext(b)
						if y == ".orig":
							binorig = tar.extractfile(f).read()
						elif y == ".mod":
							binmod = tar.extractfile(f).read()
			if xdfs!=None and binorig!=None and binmod!=None and metainfo!=None:
				tp = TunePanel(self, metainfo, xdfs, binorig, binmod)
		else:
			fbin = open(bin, "rb")
			byts = bytearray(fbin.read(os.path.getsize(bin)))
			fbin.close()
			fbin = open(xdf, "rb")
			xdfs = fbin.read(os.path.getsize(xdf))
			fbin.close()
			tp = TunePanel(self, metainfo, xdfs, byts)

	def KlineWorkerHandler(self, info, value):
		if info in ["ecmid","flashcount","dtc","dtccount","state"]:
			self.ecuinfo[info] = value
			if info == "state":
				 for a,d in self.apps.items():
					 if "enable" in d:
						 if value in d["enable"]:
							 self.appbuttons[a].Enable()
						 else:
							 self.appbuttons[a].Disable()
		elif info == "data":
			if not info in self.ecuinfo:
				self.ecuinfo[info] = {}
			self.ecuinfo[info][value[0]] = value[1:]

	def OnClose(self, event):
		self.run = False
		self.usbmonitor.join()
		self.klineworker.join()
		for w in wx.GetTopLevelWindows():
			w.Destroy()

	def OnDetectMap(self, event):
		with wx.FileDialog(self, "Open ECU dump file", wildcard="ECU dump (*.bin)|*.bin", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
			if fileDialog.ShowModal() == wx.ID_CANCEL:
				return
			pathname = fileDialog.GetPath()
			ecupn = os.path.splitext(os.path.split(pathname)[-1])[0]
			for i in ECM_IDs.values():
				if ecupn == i["pn"] and "keihinaddr" in i:
					fbin = open(pathname, "rb")
					nbyts = os.path.getsize(pathname)
					byts = bytearray(fbin.read(nbyts))
					fbin.close()
					idadr = int(i["keihinaddr"],16)
					# self.statusbar.SetStatusText("Map ID: " + byts[idadr:(idadr+7)].decode("ascii"), 0)
					self.statusbar.SetStatusText("Map ID: " + ''.join(format(bt, '02x') + ' ' for bt in byts[(idadr):(idadr + 7)]), 0)

					return
			self.statusbar.SetStatusText("Map ID: unknown", 0)

	def OnBinChecksum(self, event):
		with wx.FileDialog(self, "Open ECU dump file", wildcard="ECU dump (*.bin)|*.bin", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
			if fileDialog.ShowModal() == wx.ID_CANCEL:
				return
			pathname = fileDialog.GetPath()
			fbin = open(pathname, "rb")
			nbyts = os.path.getsize(pathname)
			byts = bytearray(fbin.read(nbyts))
			fbin.close()
			checksum = checksum8bitHonda(byts)
			self.statusbar.SetStatusText("Checksum: %s %s" % ("good" if checksum == 0 else "bad ", checksum), 0)
			return

	def OnDebug(self, event):
		self.debuglog.Show()

	def OnAppButtonClicked(self, event):
		b = event.GetEventObject()
		if not b.appid in self.appanels:
			enablestates = None
			if "enable" in self.apps[b.appid]:
				enablestates = self.apps[b.appid]["enable"]
			self.appanels[b.appid] = self.apps[b.appid]["panel"](self, b.appid, self.apps[b.appid], enablestates)
			self.appbuttons[b.appid].Disable()
		self.appanels[b.appid].Raise()

	def USBMonitorHandler(self, action, vendor, product, serial):
		dirty = False
		if action == "add":
			if not serial in self.ftdi_devices:
				self.ftdi_devices[serial] = (vendor, product)
				dirty = True
		elif action =="remove":
			if serial in self.ftdi_devices:
				if serial == self.active_ftdi_device:
					dispatcher.send(signal="FTDIDevice", sender=self, action="deactivate", vendor=vendor, product=product, serial=serial)
					self.active_ftdi_device = None
					self.__clear_data()
				del self.ftdi_devices[serial]
				dirty = True
		if len(self.ftdi_devices) > 0:
			if not self.active_ftdi_device:
				self.active_ftdi_device = list(self.ftdi_devices.keys())[0]
				dispatcher.send(signal="FTDIDevice", sender=self, action="activate", vendor=vendor, product=product, serial=serial)
				dirty = True
		else:
				pass
		if dirty:
			for i in self.devicesMenu.GetMenuItems():
				self.devicesMenu.Remove(i)
			for s in self.ftdi_devices:
				rb = self.devicesMenu.AppendRadioItem(wx.ID_ANY, "%s : %s : %s" % (self.ftdi_devices[s][0], self.ftdi_devices[s][1], s))
				self.Bind(wx.EVT_MENU, self.OnDeviceSelected, rb)
			if self.active_ftdi_device:
				self.statusbar.SetStatusText("%s : %s : %s" % (self.ftdi_devices[self.active_ftdi_device][0], self.ftdi_devices[self.active_ftdi_device][1], self.active_ftdi_device), 0)
				self.devicesMenu.FindItemByPosition(list(self.ftdi_devices.keys()).index(self.active_ftdi_device)).Check()
			else:
				self.statusbar.SetStatusText("", 0)

	def OnDeviceSelected(self, event):
		s = list(self.ftdi_devices.keys())[[m.IsChecked() for m in event.GetEventObject().GetMenuItems()].index(True)]
		if s != self.active_ftdi_device:
			if self.active_ftdi_device != None:
				dispatcher.send(signal="FTDIDevice", sender=self, action="deactivate", vendor=self.ftdi_devices[self.active_ftdi_device], product=self.ftdi_devices[self.active_ftdi_device], serial=self.active_ftdi_device)
			self.__clear_data()
			self.active_ftdi_device = s
			dispatcher.send(signal="FTDIDevice", sender=self, action="activate", vendor=self.ftdi_devices[self.active_ftdi_device], product=self.ftdi_devices[self.active_ftdi_device], serial=self.active_ftdi_device)
			self.statusbar.SetStatusText("%s : %s : %s" % (self.ftdi_devices[self.active_ftdi_device][0], self.ftdi_devices[self.active_ftdi_device][1], self.active_ftdi_device), 0)

	def AppPanelHandler(self, appid, action):
		if action == "close":
			if appid in self.appanels:
				del self.appanels[appid]
				self.appbuttons[appid].Enable()
