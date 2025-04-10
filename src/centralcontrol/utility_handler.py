#!/usr/bin/env python3

import paho.mqtt.client as mqtt
import argparse
import pickle
import threading
import queue
import serial  # for monochromator
from pathlib import Path
import sys
import logging
import pyvisa
import collections
import numpy as np
import time

# for main loop & multithreading
import gi
from gi.repository import GLib

# for logging directly to systemd journal if we can
try:
  import systemd.journal
except ImportError:
  pass

# this boilerplate code allows this module to be run directly as a script
if (__name__ == "__main__") and (__package__ in [None, '']):
  __package__ = "centralcontrol"
  # get the dir that holds __package__ on the front of the search path
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from . import virt
from .motion import motion
from .k2400 import k2400 as sm
from .illumination import illumination
from .pcb import pcb

class UtilityHandler(object):
  # for storing command messages as they arrive
  cmdq = queue.Queue()

  # for storing jobs to be worked on
  taskq = queue.Queue()

  # for outgoing messages
  outputq = queue.Queue()

  def __init__(self, mqtt_server_address='127.0.0.1', mqtt_server_port=1883):
    self.mqtt_server_address = mqtt_server_address
    self.mqtt_server_port = mqtt_server_port

    # setup logging
    self.lg = logging.getLogger(f'{__package__}.{__class__.__name__}')
    self.lg.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    logFormat = logging.Formatter(("%(asctime)s|%(name)s|%(levelname)s|%(message)s"))
    ch.setFormatter(logFormat)
    self.lg.addHandler(ch)

    # set up logging to systemd's journal if it's there
    if 'systemd' in sys.modules:
      sysL = systemd.journal.JournalHandler(SYSLOG_IDENTIFIER=self.lg.name)
      sysLogFormat = logging.Formatter(("%(levelname)s|%(message)s"))
      sysL.setFormatter(sysLogFormat)
      self.lg.addHandler(sysL)


  # The callback for when the client receives a CONNACK response from the server.
  def on_connect(self, client, userdata, flags, rc):
    self.lg.debug(f"Connected with result code {rc}")

    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    client.subscribe("cmd/#", qos=2)


  # The callback for when a PUBLISH message is received from the server.
  # this function must be fast and non-blocking to avoid estop service delay
  def handle_message(self, client, userdata, msg):
    self.cmdq.put_nowait(msg)  # pass this off for our worker to deal with


  # filters out all mqtt messages except
  # properly formatted command messages, unpacks and returns those
  # this function must be fast and non-blocking to avoid estop service delay
  def filter_cmd(self, mqtt_msg):
    result = {'cmd':''}
    try:
      msg = pickle.loads(mqtt_msg.payload)
    except Exception as e:
      msg = None
    if isinstance(msg, collections.abc.Iterable):
      if 'cmd' in msg:
        result = msg
    return(result)


  # the manager thread decides if the command should be passed on to the worker or rejected.
  # immediagely handles estops itself
  # this function must be fast and non-blocking to avoid estop service delay
  def manager(self):
    while True:
      cmd_msg = self.filter_cmd(self.cmdq.get())
      self.log_msg('New command message!',lvl=logging.DEBUG)
      if cmd_msg['cmd'] == 'estop':
        if cmd_msg['pcb_virt'] == True:
          tpcb = virt.pcb
        else:
          tpcb = pcb
        try:
          with tpcb(cmd_msg['pcb'], timeout=10) as p:
            p.query('b')
          self.log_msg('Emergency stop command issued. Re-Homing required before any further movements.', lvl=logging.INFO)
        except Exception as e:
          emsg = "Unable to emergency stop."
          self.log_msg(emsg, lvl=logging.WARNING)
          logging.exception(emsg)
      elif (self.taskq.unfinished_tasks == 0):
        # the worker is available so let's give it something to do
        self.taskq.put_nowait(cmd_msg)
      elif (self.taskq.unfinished_tasks > 0):
        self.log_msg(f'Backend busy (task queue size = {self.taskq.unfinished_tasks}). Command rejected.', lvl=logging.WARNING)
      else:
        self.log_msg(f'Command message rejected:: {cmd_msg}', lvl=logging.DEBUG)
      self.cmdq.task_done()

  # asks for the current stage position and sends it up to /response
  def send_pos(self, mo):
    pos = mo.get_position()
    payload = {'pos': pos}
    payload = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    output = {'destination':'response', 'payload': payload}  # post the position to the response channel
    self.outputq.put(output)

  # work gets done here so that we don't do any processing on the mqtt network thread
  # can block and be slow. new commands that come in while this is working will just be rejected
  def worker(self):
    while True:
      task = self.taskq.get()
      self.log_msg(f"New task: {task['cmd']} (queue size = {self.taskq.unfinished_tasks})",lvl=logging.DEBUG)
      # handle pcb and stage virtualization
      stage_pcb_class = pcb
      pcb_class = pcb
      if 'stage_virt' in task:
        if task['stage_virt'] == True:
          stage_pcb_class = virt.pcb
      if 'pcb_virt' in task:
        if task['pcb_virt'] == True:
          pcb_class = virt.pcb
      try:  # attempt to do the task
        if task['cmd'] == 'home':
          with stage_pcb_class(task['pcb'], timeout=1) as p:
            mo = motion(address=task['stage_uri'], pcb_object=p)
            mo.connect()
            mo.home()
            self.log_msg('Homing procedure complete.',lvl=logging.INFO)
            self.send_pos(mo)
          del(mo)

        # send the stage some place
        elif task['cmd'] == 'goto':
          with stage_pcb_class(task['pcb'], timeout=1) as p:
            mo = motion(address=task['stage_uri'], pcb_object=p)
            mo.connect()
            mo.goto(task['pos'])
            self.send_pos(mo)
          del(mo)

        # handle any generic PCB command that has an empty return on success
        elif task['cmd'] == 'for_pcb':
          with pcb_class(task['pcb'], timeout=1) as p:
            # special case for pixel selection to avoid parallel connections
            if (task['pcb_cmd'].startswith('s') and ('stream' not in task['pcb_cmd']) and (len(task['pcb_cmd']) != 1)):
              p.query('s')  # deselect all before selecting one
            result = p.query(task['pcb_cmd'])
          if result == '':
            self.log_msg(f"Command acknowledged: {task['pcb_cmd']}", lvl=logging.DEBUG)
          else:
            self.log_msg(f"Command {task['pcb_cmd']} not acknowleged with {result}", lvl=logging.WARNING)

        # get the stage location
        elif task['cmd'] == 'read_stage':
          with stage_pcb_class(task['pcb'], timeout=1) as p:
            mo = motion(address=task['stage_uri'], pcb_object=p)
            mo.connect()
            self.send_pos(mo)
          del(mo)

        # zero the mono
        elif task['cmd'] == 'mono_zero':
          if task['mono_virt'] == True:
            self.log_msg("0 GOTO virtually worked!", lvl=logging.INFO)
            self.log_msg("1 FILTER virtually worked!", lvl=logging.INFO)
          else:
            with serial.Serial(task['mono_address'], 9600, timeout=1) as mono:
              mono.write("0 GOTO")
              self.log_msg(mono.readline.strip(), lvl=logging.INFO)
              mono.write("1 FILTER")
              self.log_msg(mono.readline.strip(), lvl=logging.INFO)

        elif task['cmd'] == 'spec':
          if task['le_virt'] == True:
            le = virt.illumination(address=task['le_address'], default_recipe=task['le_recipe'])
          else:
            le = illumination(address=task['le_address'], default_recipe=task['le_recipe'], connection_timeout=1)
          con_res = le.connect()
          if con_res == 0:
            response = {}
            int_res = le.set_intensity(task['le_recipe_int'])
            if int_res == 0:
              response["data"] = le.get_spectrum()
              response["timestamp"] = time.time()
              output = {'destination':'calibration/spectrum', 'payload': pickle.dumps(response)}
              self.outputq.put(output)
            else:
              self.log_msg(f'Unable to set light engine intensity.',lvl=logging.INFO)
          else:
            self.log_msg(f'Unable to connect to light engine.',lvl=logging.INFO)
          del(le)

        # device round robin commands
        elif task['cmd'] == 'round_robin':
          if len(task['slots']) > 0:
            with pcb_class(task['pcb'], timeout=1) as p:
              p.query('iv') # make sure the circuit is in I-V mode (not eqe)
              p.query('s') # make sure we're starting with nothing selected
              if task['smu_virt'] == True:
                smu = virt.k2400
              else:
                smu = sm
              k = smu(addressString=task['smu_address'], terminator=task['smu_le'], serialBaud=task['smu_baud'], front=False)

              # set up sourcemeter for the task
              if task['type'] == 'current':
                pass  # TODO: smu measure current command goes here
              elif task['type'] == 'rtd':
                k.setupDC(auto_ohms=True)
              elif task['type'] == 'connectivity':
                self.log_msg(f'Checking connections. Only failures will be printed.',lvl=logging.INFO)
                k.set_ccheck_mode(True)

              for i, slot in enumerate(task['slots']):
                dev = task['pads'][i]
                mux_string = task['mux_strings'][i]
                p.query(mux_string)  # select the device
                if task['type'] == 'current':
                  pass  # TODO: smu measure current command goes here
                elif task['type'] == 'rtd':
                  m = k.measure()[0]
                  ohm = m[2]
                  if (ohm < 3000) and (ohm > 500):
                    self.log_msg(f'{slot} -- {dev} Could be a PT1000 RTD at {self.rtd_r_to_t(ohm):.1f} °C',lvl=logging.INFO)
                elif task['type'] == 'connectivity':
                  if k.contact_check() == False:
                    self.log_msg(f'{slot} -- {dev} appears disconnected.',lvl=logging.INFO)
                p.query(f"s{slot}0") # disconnect the slot

              if task['type'] == 'connectivity':
                k.set_ccheck_mode(False)
                self.log_msg(f'Contact check complete.',lvl=logging.INFO)
              elif task['type'] == 'rtd':
                self.log_msg(f'Temperature measurement complete.',lvl=logging.INFO)
                k.setupDC(sourceVoltage=False)
              p.query("s")
              del(k)
      except Exception as e:
        self.log_msg(e, lvl=logging.WARNING)
        logging.exception(e)
        try:
          del(le)  # ensure le is cleaned up
        except:
          pass
        try:
          del(mo)  # ensure mo is cleaned up
        except:
          pass
        try:
          del(k)  # ensure k is cleaned up
        except:
          pass

      # system health check
      if task['cmd'] == 'check_health':
        rm = pyvisa.ResourceManager('@py')
        if 'pcb' in task:
          self.log_msg(f"Checking controller@{task['pcb']}...",lvl=logging.INFO)
          try:
            with pcb_class(task['pcb'], timeout=1) as p:
              self.log_msg('Controller connection initiated',lvl=logging.INFO)
              self.log_msg(f"Controller firmware version: {p.firmware_version}",lvl=logging.INFO)
              self.log_msg(f"Controller axes: {p.detected_axes}",lvl=logging.INFO)
              self.log_msg(f"Controller muxes: {p.detected_muxes}",lvl=logging.INFO)
          except Exception as e:
            emsg = f'Could not talk to control box'
            self.log_msg(emsg, lvl=logging.WARNING)
            logging.exception(emsg)

        if 'psu' in task:
          self.log_msg(f"Checking power supply@{task['psu']}...",lvl=logging.INFO)
          if task['psu_virt'] == True:
            self.log_msg(f'Power supply looks virtually great!',lvl=logging.INFO)
          else:
            try:
              with rm.open_resource(task['psu']) as psu:
                self.log_msg('Power supply connection initiated',lvl=logging.INFO)
                idn = psu.query("*IDN?")
                self.log_msg(f'Power supply identification string: {idn.strip()}',lvl=logging.INFO)
            except Exception as e:
              emsg = f'Could not talk to PSU'
              self.log_msg(emsg, lvl=logging.WARNING)
              logging.exception(emsg)

        if 'smu_address' in task:
          self.log_msg(f"Checking sourcemeter@{task['smu_address']}...",lvl=logging.INFO)
          if task['smu_virt'] == True:
            self.log_msg(f'Sourcemeter looks virtually great!',lvl=logging.INFO)
          else:
            # for sourcemeter
            open_params = {}
            open_params['resource_name'] = task['smu_address']
            open_params['timeout'] = 300 # ms
            if 'ASRL' in open_params['resource_name']:  # data bits = 8, parity = none
              open_params['read_termination'] = task['smu_le']  # NOTE: <CR> is "\r" and <LF> is "\n" this is set by the user by interacting with the buttons on the instrument front panel
              open_params['write_termination'] = "\r" # this is not configuable via the instrument front panel (or in any way I guess)
              open_params['baud_rate'] = task['smu_baud']  # this is set by the user by interacting with the buttons on the instrument front panel
              open_params['flow_control'] = pyvisa.constants.VI_ASRL_FLOW_RTS_CTS # user must choose NONE for flow control on the front panel
            elif 'GPIB' in open_params['resource_name']:
              open_params['write_termination'] = "\n"
              open_params['read_termination'] = "\n"
              # GPIB takes care of EOI, so there is no read_termination
              open_params['io_protocol'] = pyvisa.constants.VI_HS488  # this must be set by the user by interacting with the buttons on the instrument front panel by choosing 488.1, not scpi
            elif ('TCPIP' in open_params['resource_name']) and ('SOCKET' in open_params['resource_name']):
              # GPIB <--> Ethernet adapter
              pass

            try:
              with rm.open_resource(**open_params) as smu:
                self.log_msg('Sourcemeter connection initiated',lvl=logging.INFO)
                idn = smu.query("*IDN?")
                self.log_msg(f'Sourcemeter identification string: {idn}',lvl=logging.INFO)
            except Exception as e:
              emsg = f'Could not talk to sourcemeter'
              self.log_msg(emsg, lvl=logging.WARNING)
              logging.exception(emsg)

        if 'lia_address' in task:
          self.log_msg(f"Checking lock-in@{task['lia_address']}...",lvl=logging.INFO)
          if task['lia_virt'] == True:
            self.log_msg(f'Lock-in looks virtually great!',lvl=logging.INFO)
          else:
            try:
              with rm.open_resource(task['lia_address'], baud_rate=9600) as lia:
                lia.read_termination = '\r'
                self.log_msg('Lock-in connection initiated',lvl=logging.INFO)
                idn = lia.query("*IDN?")
                self.log_msg(f'Lock-in identification string: {idn.strip()}',lvl=logging.INFO)
            except Exception as e:
              emsg = f'Could not talk to lock-in'
              self.log_msg(emsg, lvl=logging.WARNING)
              logging.exception(emsg)

        if 'mono_address' in task:
          self.log_msg(f"Checking monochromator@{task['mono_address']}...",lvl=logging.INFO)
          if task['mono_virt'] == True:
            self.log_msg(f'Monochromator looks virtually great!',lvl=logging.INFO)
          else:
            try:
              with rm.open_resource(task['mono_address'], baud_rate=9600) as mono:
                self.log_msg('Monochromator connection initiated',lvl=logging.INFO)
                qu = mono.query("?nm")
                self.log_msg(f'Monochromator wavelength query result: {qu.strip()}',lvl=logging.INFO)
            except Exception as e:
              emsg = f'Could not talk to monochromator'
              self.log_msg(emsg, lvl=logging.WARNING)
              logging.exception(emsg)

        if 'le_address' in task:
          self.log_msg(f"Checking light engine@{task['le_address']}...", lvl=logging.INFO)
          le = None
          if task['le_virt'] == True:
            ill = virt.illumination
          else:
            ill = illumination
          try:
            le = ill(address=task['le_address'], default_recipe=task['le_recipe'], connection_timeout=1)
            con_res = le.connect()
            if con_res == 0:
              self.log_msg('Light engine connection successful', lvl=logging.INFO)
            elif (con_res == -1):
              self.log_msg("Timeout waiting for wavelabs to connect", lvl=logging.WARNING)
            else:
              self.log_msg(f"Unable to connect to light engine and activate {task['le_recipe']} with error {con_res}", lvl=logging.WARNING)
          except Exception as e:
            emsg = f'Light engine connection check failed: {e}'
            self.log_msg(emsg,lvl=logging.WARNING)
            logging.exception(emsg)
          try:
            del(le)
          except:
            pass

      self.taskq.task_done()


  # send up a log message to the status channel
  def log_msg(self, msg, lvl=logging.DEBUG):
    self.lg.info(f'Message to client: {msg}')
    payload = {'log':{'level':lvl, 'text':msg}}
    payload = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    output = {'destination':'status', 'payload': payload}
    self.outputq.put(output)


  # thread that publishes mqtt messages on behalf of the worker and manager
  def sender(self, mqttc):
    while True:
      to_send = self.outputq.get()
      mqttc.publish(to_send['destination'], to_send['payload'], qos=2).wait_for_publish()
      self.outputq.task_done()

  # converts RTD resistance to temperature. set r0 to 100 for PT100 and 1000 for PT1000
  def rtd_r_to_t(self, r, r0=1000, poly=None):
    PTCoefficientStandard = collections.namedtuple("PTCoefficientStandard", ["a", "b", "c"])
    # Source: http://www.code10.info/index.php%3Foption%3Dcom_content%26view%3Darticle%26id%3D82:measuring-temperature-platinum-resistance-thermometers%26catid%3D60:temperature%26Itemid%3D83
    ptxIPTS68 = PTCoefficientStandard(+3.90802e-03, -5.80195e-07, -4.27350e-12)
    ptxITS90 = PTCoefficientStandard(+3.9083E-03, -5.7750E-07, -4.1830E-12)
    standard = ptxITS90  # pick an RTD standard
    
    noCorrection = np.poly1d([])
    pt1000Correction = np.poly1d([1.51892983e-15, -2.85842067e-12, -5.34227299e-09, 1.80282972e-05, -1.61875985e-02, 4.84112370e+00])
    pt100Correction = np.poly1d([1.51892983e-10, -2.85842067e-08, -5.34227299e-06, 1.80282972e-03, -1.61875985e-01, 4.84112370e+00])

    A, B = standard.a, standard.b

    if poly is None:
      if abs(r0 - 1000.0) < 1e-3:
        poly = pt1000Correction
      elif abs(r0 - 100.0) < 1e-3:
        poly = pt100Correction
      else:
        poly = noCorrection

    t = ((-r0 * A + np.sqrt(r0 * r0 * A * A - 4 * r0 * B * (r0 - r))) / (2.0 * r0 * B))
    
    # For subzero-temperature refine the computation by the correction polynomial
    if r < r0:
      t += poly(r)
    return t

  def run(self):
    self.loop = GLib.MainLoop.new(None, False)

    # start the manager (decides what to do with commands from mqtt)
    threading.Thread(target=self.manager, daemon=True).start()

    # start the worker (does tasks the manger tells it to)
    threading.Thread(target=self.worker, daemon=True).start()

    self.client = mqtt.Client()
    self.client.on_connect = self.on_connect
    self.client.on_message = self.handle_message

    # connect to the mqtt server
    self.client.connect(self.mqtt_server_address, port=self.mqtt_server_port, keepalive=60)

    # start the sender (publishes messages from worker and manager)
    threading.Thread(target=self.sender, args=(self.client,)).start()

    # Blocking call that processes network traffic, dispatches callbacks and
    # handles reconnecting.
    # Other loop*() functions are available that give a threaded interface and a
    # manual interface.
    self.client.loop_forever()

def main():
  parser = argparse.ArgumentParser(description='Utility handler')
  parser.add_argument('-a', '--address', type=str, default='127.0.0.1', const="127.0.0.1", nargs='?', help='ip address/hostname of the mqtt server')
  parser.add_argument('-p', '--port', type=int, default=1883, help="MQTT server port")
  args = parser.parse_args()

  u = UtilityHandler(mqtt_server_address=args.address, mqtt_server_port=args.port)
  u.run()

if __name__ == "__main__":
  main()