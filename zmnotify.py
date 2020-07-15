'''
Appdaemon App to run under Home Assistant Appdaemon

Registers for state change notification on the specified sensors.
These sensors are MQTT data as generated from Zoneminder ES with the
MQTT option enabled. Info from this is massaged into a text message
and then attaches a image frame for the Zoneminder Event Id.

**NOTE:** This is a work in progress.
'''
import glob
import os
import traceback
import appdaemon.plugins.hass.hassapi as hass
import pyzm.api as zmAPI
import pyzm.helpers as zmtypes
import requests
from datetime import datetime as dt
import logging

__version__ = '0.3.7'


def versiontuple(v):
    return tuple(map(int, (v.split("."))))

class ZmLogger:
    """
    The pyzm module uses a non-standard logging API. It also defaults to debug level. This is a simple
    wrapper class to bridge between the pyzm logging such that log level is adhered to.
    """

    def __init__(self, logger):
        self.logger = logger

    def Debug (self, level, message, caller=None):
        self.logger.log(logging.DEBUG, message)

    def Info (self,message, caller=None):
        self.logger.log(logging.INFO, message)

    def Error (self,message, caller=None):
        self.logger.log(logging.ERROR, message)

    def Fatal (self,message, caller=None):
        self.logger.log(logging.FATAL, message)

    def Panic (self,message, caller=None):
        self.logger.log(logging.FATAL, message)



class ZmMonitor:
    AUDIT_TIMEOUT = 60

    """
    Wrapper for Zoneminder Monitor object.
    This tracks the current state of the monitor e.g. Nodect, Modect, None etc.
    The user should specify what the enabled state should be e.g. Modect.
    Instances of this class are used to 'turn off' the camera when the associated notify gate is off.
    """

    def __init__(self, ad_parent, mo, function, options, logger):
        self._zmapi = ad_parent.zm_api
        self._ad = ad_parent
        self._zm_monitor = mo
        self._settings = {}
        self.logger = logger
        for key, value in options.items():
            self._settings[key] = options[key]
        self._settings['function'] = function
        # get the current state according to zoneminder
        self._zm_function = mo.function()
        self.log("Monitor ({}) is reporting function {}".format(mo.name(), self._zm_function))
        # audit state every 2 minutes
        self._audit_timer = self._ad.run_in(self.audit_monitor_state, self.AUDIT_TIMEOUT)

    def log(self, msg, *args, **kwargs):
        level = logging.INFO
        self.logger.log(level, msg, *args, **kwargs)

    @property
    def id(self):
        return self._zm_monitor.id()

    @property
    def name(self):
        return self._zm_monitor.name()

    def find_event(self, event_id, start_time='1 hour ago'):
        evid = int(event_id)
        options = {}
        options['from'] = start_time
        for retry in range(0, 2):
            try:
                event_list = self._zm_monitor.events(options).list()
                break
            except requests.HTTPError:
                self.log("Received HTTPError from Zoneminder server, retry: {}".format(retry))
            except TypeError:
                self.log("Received Type Error from Zoneminder API, retry: {}".format(retry))
        if event_list is None:
            return None
        self.log("ZM Monitor ({}) reporting {} events".format(self._zm_monitor.name(), len(event_list)))
        # return next((x for x in event_list if x.id() == event_id), None)
        rv = None
        for x in event_list:
            if x.id() == evid:
                rv = x
                break
        return rv

    def set_zoneminder_state(self, function):
        options = {'function': function}
        self.log("Monitor {}: sending zoneminder request to set function state to: {}".format(self.name, function))
        self._zm_monitor.set_parameter(options)

    def set_function_state(self, function):
        if function != self._zm_function:
            self.log(
                "ZM Monitor ({}) changing func from {} to {}".format(self._zm_monitor.name(), self._zm_function,
                                                                     function))
            self.set_zoneminder_state(function)
            self._zm_function = function

    def enable_function(self):
        self.set_function_state(self._settings['function'])

    def audit_monitor_state(self, kwargs):
        """
        Check that zoneminder reported state matches our state
        If there is a mismatch, force zoneminder to our set state.
        We are limited here given the current pyzm monitor proxy caches the state of the monitor-function,
        the assumption on the part of pyzm is there will never be a need to query to the real current running
        state of the monitor other than the status() which only reports whether if the monitor is actively running.
        Consequently, we will assume that 'not running' is equivalent to function mode set to None.
        :return:
        """
        status = None
        for retry in range(0, 1):
            try:
                status = self._zm_monitor.status()  # query running status of monitor
            except requests.exceptions.HTTPError as err:
                self.log("Audit monitor status request failed")
            except TypeError as err:
                self.log("Audit monitor status request failed likely to expired token?")
        if status is None:
            return
        is_running = status['status']
        # self.log("Audit state of monitor {}, zm reports state: {}".format(self.name, status['statustext']))
        if not is_running and self._zm_function != 'None':
            self.log("Monitor state mismatch detected by audit, "
                     "set ZM camera {} to {}".format(self.name, self._zm_function))
            self.set_zoneminder_state(self._zm_function)
        else:
            self.log("Monitor ZM Camera {} audit passed: {}".format(self.name, self._zm_function))
        self._audit_timer = self._ad.run_in(self.audit_monitor_state, self.AUDIT_TIMEOUT)


class HASensor:
    """
    Home Assistant Sensor. This is a proxy for the sensor defined in Home Assistant for the
    Zoneminder monitor. This sensor should be a MQTT sensor.
    """

    def __init__(self, ad_parent, name, attributes, logger):
        self._ad = ad_parent
        self._name = name
        self.logger = logger
        zm_monitor = None
        self._cntrl_data = None
        self._event_cnt = 0
        self._window_timer = None
        # monitor_squelched indicates an active throttle is ongoing for this sensor
        # squelching true stops the forwarding of notifications to HA notify service
        self._monitor_squelched = False
        mfnc = 'None'
        for key, value in attributes.items():
            if key == 'zm_monitor':
                mname = value['name']
                mfnc = value['function']
                zm_monitor = self._ad.zm_api.monitors().find(name=mname)
                if zm_monitor is None:
                    self.log('Failed to find Zoneminder monitor: {}'.format(mname))
            elif key == 'zm_control':
                self.log("Sensor ({}) adding control settings".format(self.name))
                self._cntrl_data = attributes[key]
                self.log("Sensor ({}) allow: {}".format(self.name, self._cntrl_data["allow"]))
            elif key == 'ha_gate':
                # this is the boolean gate controlled by the user
                # when true, the monitor function will be set to None if permission is given
                self._gate = attributes[key]
        self._allow_monitor_control = True if self._cntrl_data['allow'] else False
        for key, value in self._cntrl_data["ratelimit"].items():
            if key == 'cnt':
                self._cntrl_data["ratelimit"][key] = int(value)

        if zm_monitor is not None:
            self._monitor = ZmMonitor(ad_parent, zm_monitor, mfnc, self._cntrl_data, logger)
        else:
            raise TypeError

        self._current_gate_state = self._ad.get_state(self._gate)
        self.log("{} is currently {}".format(self._gate, self._current_gate_state))
        if self._allow_monitor_control and self._current_gate_state == "off":
            self._monitor.set_function_state('None')
        elif self._allow_monitor_control and self._current_gate_state == "on":
            self._monitor.enable_function()

        self._ad.listen_state(self.handle_state_change, self._gate)

    def log(self, msg, *args, **kwargs):
        level = logging.INFO
        self.logger.log(level, msg, *args, **kwargs)

    @property
    def name(self):
        return self._name

    @property
    def ha_gate(self):
        return self._gate

    def monitor(self):
        return self._monitor

    def monitor_id(self):
        return self._monitor.id()

    def squelched(self):
        return self._monitor_squelched

    def is_notify_enabled(self):
        return self._current_gate_state == "on"

    def reset_squelch(self):
        self._event_cnt = 0
        if self._window_timer is not None:
            self._ad.cancel_timer(self._window_timer)
        self._window_timer = None
        if self._monitor_squelched:
            self._monitor_squelched = False
            self.log("Sensor {} squelch off".format(self.name))
            # self._ad.set_state(self._gate, state="on")

    def set_squelch(self):
        if self.is_notify_enabled() and self._window_timer is not None:
            self.log("Sensor {} squelched at event cnt: {}".format(self.name, self._event_cnt))
            # self._ad.set_state(self._gate, state="off")
            self._monitor_squelched = True

    def handle_state_change(self, entity, attribute, old, new, kwargs):
        """
        Callback hook called on input_boolean notify gate.
        The notify gate is a simple user button that can be turned on or off to easily
        enable or disable processing zoneminder events.

        :param entity: name of HA entity that has changed state
        :param attribute:
        :param old: previous state (off or on)
        :param new: new/current state of off or on
        :param kwargs:
        """
        self.log("Sensor notify gate state change reported on {} from {} to {}".format(self._gate, old, new))
        if self._allow_monitor_control:
            if new == "on":
                self._monitor.enable_function()
            elif new == "off":
                self._monitor.set_function_state('None')
                # if user manually turns off notify gate, then should clear squelch
                # otherwise notify gate will be turned back on with timer expires
                # self.reset_squelch()
            else:
                self.log("ERROR: unexpected state change to {}".format(new))
        self._current_gate_state = new

    def process_event(self):
        self._event_cnt += 1
        rt = self._cntrl_data["ratelimit"]
        if self._window_timer is None:
            self._window_timer = self._ad.run_in(self.handle_window_timer, rt["window"])
        if self._event_cnt > rt['cnt']:
            self.set_squelch()

    def handle_window_timer(self, kwargs):
        self._window_timer = None
        self.log("Rate limit window timer expired on sensor {}".format(self.name))
        self.reset_squelch()


# noinspection PyAttributeOutsideInit
class ZmEventNotifier(hass.Hass):
    """
    Appdaemon class.
    """
    zm_api: zmAPI.ZMApi
    img_types = ['jpg', 'gif', 'png', 'tif', 'svg', 'jpeg']
    ts_fmt = '%a %I:%M %p'
    log_header = 'ZM ES Handler'
    CLEANUP_INTERVAL = 24*60*60

    def init(self):
        self._version = __version__
        self.notify_occup_list = []
        self.notify_unoccup_list = []
        self.occupied_state = False
        self.notify_list = self.notify_unoccup_list
        self.zm_options = {
            'apiurl': None,
            'portalurl': None,
            'user': None,
            'password': None,
            'logger': None,  # 'logger': None # use none if you don't want to log to ZM
            'token': True
        }
        self.sensors = {}
        self.zm_api = None
        self.img_width = 600
        self.img_cache_dir = '/tmp'
        self.zm_monitors = None
        self.cache_file_cnt = 0
        self.last_cleanup = dt.now()

    @staticmethod
    def version():
        return __version__

    def initialize(self):
        """
        initialize() function which will be called at startup and reload
        """
        self.log('{} initializing version {}'.format(self.log_header, self.version()))
        self.init()
        try:
            self.zm_options['apiurl'] = self.args["zm_url"] + self.args["zmapi_loc"]
            self.zm_options['portalurl'] = self.args["zm_url"]
            self.zm_options['user'] = self.args["zm_user"]
            self.zm_options['password'] = self.args["zm_pw"]
            self.zm_options['logger'] = ZmLogger(self.logger)
            self.img_width = self.args["img_width"]
            self.img_cache_dir = self.args["img_cache_dir"]
            self.img_frame_type = self.args["img_frame_type"]
            self.txt_blk_list = self.args["txt_blk_list"]

            for notify_id in self.args["notify-occupied"]:
                if notify_id is list:
                    self.notify_occup_list.extend(notify_id)
                else:
                    self.notify_occup_list.append(notify_id)
                    self.log("adding occupied notifier: {}".format(notify_id))
            for notify_id in self.args["notify-unoccupied"]:
                if notify_id is list:
                    self.notify_unoccup_list.extend(notify_id)
                else:
                    self.notify_unoccup_list.append(notify_id)
                    self.log("adding unoccupied notifier: {}".format(notify_id))

            if not self.args["zmapi_use_token"]:
                self.zm_options['token'] = False
        except KeyError:
            self.log("Missing arguments in yaml setup file")
            raise
        self.clean_files_in_local_cache()
        if self.zm_api is None:
            for retry in range(0, 2):
                try:
                    self.zm_api = zmAPI.ZMApi(options=self.zm_options)
                except requests.HTTPError:
                    self.error("Encountered HTTPError, retrying, retry cnt: {}".format(retry))
                if self.zm_api is not None:
                    break
            if self.zm_api is None:
                self.error("Failed to connect to Zoneminder, aborting")
                return
            try:
                version_info = self.zm_api.version()
                if version_info is not None and version_info['status'] == 'ok':
                    self.log("Connected to Zoneminder server reporting"
                             " version {}".format(version_info['zm_version']))
                    self.log("API pyzm reporting version {}".format(version_info['api_version']))
                else:
                    self.error("Failed to retrieve version info for Zoneminder")
            except Exception as e:
                self.error('Error: {}'.format(str(e)))
                self.error(traceback.format_exc())
                raise
        occupied_bool = self.args["occupied"]
        self.occupied_state = True if self.get_state(occupied_bool) == 'on' else False
        self.listen_state(self.handle_occupied_state_change, occupied_bool)
        if self.occupied_state:
            self.notify_list = self.notify_occup_list
            self.log("Setting to {} notify list, len={}".format("occupied", len(self.notify_list)))

        # sensors is a dict of sensorid with associated notify gate
        for sensor in self.args["sensors"]:
            new_sensor = "sensor." + sensor
            self.log("adding listener for sensor {}".format(new_sensor))
            self.sensors[new_sensor] = HASensor(self, new_sensor, self.args["sensors"][sensor], self.logger)
            self.listen_state(self.handle_state_change, new_sensor)

        # at this point we should authenticated with zoneminder
        self.log('Zoneminder ES Handler init completed')

    def clean_files_in_local_cache(self):
        exp = self.img_cache_dir + "/*[.jpeg,.jpg]"
        file_list = glob.glob(exp)
        delete_cnt = 0
        if len(file_list) > 0:
            self.log("Cleaning cache dir: {} removing {} files ".format(self.img_cache_dir, len(file_list)))
        for filePath in file_list:
            fdelta = dt.now() - dt.fromtimestamp(os.stat(filePath).st_mtime)
            age_of_file_in_secs = fdelta.total_seconds()
            if age_of_file_in_secs > 30:
                try:
                    os.remove(filePath)
                    delete_cnt += 1
                except:
                    pass
        self.last_cleanup = dt.now()
        return delete_cnt

    def get_fid(self, frame_code):
        fid_map = {'a': "alarm",
                   's': "snapshot",
                   'o': "objdetect"
                   }
        if frame_code in fid_map:
            return fid_map[frame_code]
        else:
            self.log("invalid frame code {}".format(frame_code))
            raise TypeError

    @staticmethod
    def clean_text_msg(txt_msg, blk_list):
        r_txt = txt_msg
        for item in blk_list:
            r_txt = r_txt.replace(item, '')
        return r_txt

    def handle_occupied_state_change(self, entity, attribute, old, new, kwargs):
        self.log('processing state change for entity: {} to state {}'.format(entity, new))
        if new == "on":
            occupied = True
        else:
            occupied = False
        self.occupied_state = occupied
        if occupied:
            self.notify_list = self.notify_occup_list
            self.log("Setting to {} notify list, len={}".format("occupied", len(self.notify_list)))
        else:
            self.notify_list = self.notify_unoccup_list
            self.log("Setting to {} notify list, len={}".format("unoccupied", len(self.notify_list)))


    def handle_state_change(self, entity, attribute, old, new, kwargs):
        """
        generate notifications for camera motion
        new state string from zoneminder will be formatted as
          "driveway hires:(503) [a] detected:car:78% Linked"
           camera-name:(event-id) [frame-type] "object detect message"
        :param entity: sensor with state change
        :param attribute:
        :param old: previous state
        :param new: new state
        :param kwargs:
        :return: None
        """
        # is the notify gate on for the camera reporting object detection?
        zm_sensor: HASensor = self.sensors[entity]
        if not zm_sensor.squelched():
            zm_sensor.process_event()

        if self.get_state(zm_sensor.ha_gate) == 'on':
            if not zm_sensor.squelched():
                self.log('processing state change for entity: {}'.format(entity))
                # gate is on, so proceed with notifications
                timestamp = dt.now().strftime(self.__class__.ts_fmt)
                camera, detail = new.split(':(')
                event_id, sub_detail = detail.split(') ')
                frame, txt_body = sub_detail.split('] ')
                txt_body = self.clean_text_msg(txt_body, self.txt_blk_list)
                msg_title = '{} Camera alert @{}\n'.format(camera, timestamp)
                fid = frame[1:]
                zm_event: zmtypes.Event = zm_sensor.monitor().find_event(event_id)
                if zm_event is not None:
                    self.log("found ZM Event ({}) for id {}".format(zm_event.name(), zm_event.id()))
                else:
                    self.error("failed to find ZM Event for id {}, aborting".format(event_id))
                    return
                # attempt to pull the image based on the configured frame type
                # but if not available, pull the type indicated in the name/msg field
                ftl = [self.img_frame_type, fid]
                ft_min_set = [i for n, i in enumerate(ftl) if i not in ftl[:n]]

                attempt = 1
                for entry in ft_min_set:
                    self.log("Attempt #({}): pull image file with fid: {}".format(attempt, entry))
                    frame_type = self.get_fid(entry)
                    zm_event.download_image(fid=frame_type, dir=self.img_cache_dir)
                    img_filename = "{}-{}.jpg".format(zm_event.id(), frame_type)
                    img_file_uri = os.path.join(self.img_cache_dir, img_filename)
                    self.cache_file_cnt += 1
                    if os.path.exists(img_file_uri):
                        for notifier in self.notify_list:
                            nlist = notifier.split(',')
                            notify_path = nlist[0]
                            notify_entity = None
                            if len(nlist) > 1:
                                notify_entity = nlist[1]
                            try:
                                if notify_path.startswith('notify/'):
                                    self.log(
                                        "ZM ES Handler: sending text to {} for event: {}".format(notify_path, event_id))
                                    # currently relying on a hint embedded in the name of the notify path
                                    if "hangout" in notify_path:
                                        self.call_service(notify_path, message=txt_body, title=msg_title,
                                                          data={'image_file': img_file_uri})
                                    else:
                                        # fall through to here with a simple call to send text with an image
                                        # need to explore if these entities can be queried on type to descriminate
                                        # how to make the service call
                                        msg_txt = msg_title + txt_body
                                        self.call_service(notify_path, message=msg_txt)
                                elif notify_path.startswith('tts/') and notify_entity is not None:
                                    self.log("ZM ES Handler: announcing text via tts to {} for event: {}".format(
                                        notify_entity, event_id))
                                    info_txt = txt_body.split(':')
                                    announce_text = "{} camera {}".format(camera, " ".join(info_txt))
                                    self.call_service(notify_path, entity_id=notify_entity, message=announce_text)
                                else:
                                    self.log("Dropping notification to {}".format(notify_path))
                            except:
                                self.log("Exception encountered on calling entity {}".format(notify_path))
                                pass
                        break
                    else:
                        self.log("Failed to pull Zoneminder image for event id:{} camera: {} msg: {}".format(event_id,
                                                                                                             camera,
                                                                                                             txt_body))
                        attempt += 1
                cleanup_delta = dt.now() - self.last_cleanup
                if cleanup_delta.total_seconds() >= self.CLEANUP_INTERVAL:
                    self.cache_file_cnt -= self.clean_files_in_local_cache()
            else:
                self.log("ZM ES Handler: squelch active for entity: {}".format(entity))
        else:
            self.log("ZM ES Handler: notify gate is turned off for entity: {}".format(entity))
        return
