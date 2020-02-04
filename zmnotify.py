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
from io import open as iopen
from datetime import datetime as dt
import logging

__version__ = '0.2'


def versiontuple(v):
    return tuple(map(int, (v.split("."))))


class Zmes(zmAPI.ZMApi):
    """
    Zoneminder Event Server Proxy
    This is a placeholder until the ZMApi class and pyzm helpers
    are extended to support the functions provided by this class.
    """

    def __init__(self, options={}):
        super(Zmes, self).__init__(options)

    def _make_binary_request(self, url=None, query={}, payload={}, req_type='get', reauth=True):
        """
    Issue http request and allow for binary/raw response data.
    This is copy of the parent class _make_request method but returns a raw response.
    :param url:
    :param query:
    :param payload:
    :param req_type: one of ['get', 'post', 'put', 'delete']
    :param reauth: True if authentication redo
    :return: requests.response
    """
        req_type = req_type.lower()
        if self._versiontuple(self.api_version) >= self._versiontuple('2.0'):
            query['token'] = self.access_token
            # ZM 1.34 API bug, will be fixed soon
            self.session = requests.Session()
        else:
            # credentials is already query formatted
            lurl = url.lower()
            if lurl.endswith('json') or lurl.endswith('/'):
                qchar = '?'
            else:
                qchar = '&'
            url += qchar + self.legacy_credentials

        try:
            self.logger.Debug(1, 'make_request called with url={} payload={} type={} query={}'.format(url, payload,
                                                                                                      req_type,
                                                                                                      query))
            if req_type == 'get':
                r = self.session.get(url, params=query)
            elif req_type == 'post':
                r = self.session.post(url, data=payload, params=query)
            elif req_type == 'put':
                r = self.session.put(url, data=payload, params=query)
            elif req_type == 'delete':
                r = self.session.delete(url, data=payload, params=query)
            else:
                self.logger.Error('Unsupported request type:{}'.format(req_type))
                raise ValueError('Unsupported request type:{}'.format(req_type))
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as err:
            self.logger.Debug(1, 'Got API access error: {}'.format(err), 'error')
            if err.response.status_code == 401 and reauth:
                self.logger.Debug(1, 'Retrying login once')
                self._relogin()
                self.logger.Debug(1, 'Retrying failed request')
                return self._make_binary_request(url, query, payload, req_type, reauth=False)
            else:
                raise err

    def get_event_image_data(self, event_id, fid='alarm', px_width=600, query={},
                             api_loc_suffix='/api',
                             reauth=True):
        """
        Pull the event image file

        :rtype: object
        :param reauth: redo authentication if true
        :param api_loc_suffix: api_url suffix that is appended to the base url
        :param suffix_list: list of image types to pull
        :param event: event object defining the notification event
        :param fid: frame identifier, one of alarm, objectdetect, or integer frame number
        :param px_width: scale image to this number of pixels
        :param query: http request query options
        """
        # build url for the http get request to retrieve the image file
        sfx_len = -len(api_loc_suffix)
        img_url = self.api_url[0:sfx_len] + '/index.php?view=image&eid={}&fid={}&width={}'.format(event_id, fid,
                                                                                                  px_width)
        query['token'] = self.access_token
        rsp = self._make_binary_request(img_url, query=query)
        return rsp


class ZmMonitor:
    """
    Wrapper for Zoneminder Monitor object.
    This tracks the current state of the monitor e.g. Nodect, Modect, None etc.
    The user should specify what the enabled state should be e.g. Modect.
    Instances of this class are used to 'turn off' the camera when the associated notify gate is off.
    """

    def __init__(self, zmapi, mo, function, options, logger):
        self._zmapi = zmapi
        self._zm_monitor = mo
        self._settings = {}
        self.logger = logger
        for key, value in options.items():
            self._settings[key] = options[key]
        self._settings['function'] = function
        self._zm_function = mo.function()
        self.log("Monitor ({}) is reporting function {}".format(mo.name(), self._zm_function))

    def log(self, msg, *args, **kwargs):
        level = logging.INFO
        self.logger.log(level, msg, *args, **kwargs)

    def id(self):
        return self._zm_monitor.id()

    def find_event(self, event_id, start_time='1 hour ago'):
        evid = int(event_id)
        options = {}
        options['from'] = start_time
        event_list = self._zm_monitor.events(options).list()
        self.log("ZM Monitor ({}) reporting {} events".format(self._zm_monitor.name(), len(event_list)))
        # return next((x for x in event_list if x.id() == event_id), None)
        rv = None
        for x in event_list:
            if x.id() == evid:
                rv = x
                break
        return rv

    def set_function_state(self, function):
        if function != self._zm_function:
            options = {'function': function}
            self.log(
                "ZM Monitor ({}) changing func from {} to {}".format(self._zm_monitor.name(), self._zm_function,
                                                                     function))
            self._zm_monitor.set_parameter(options)
            self._zm_function = function

    def enable_function(self):
        self.set_function_state(self._settings['function'])


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
        self._monitor_squelched = False
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
                self._gate = attributes[key]
        self._allow_monitor_control = True if self._cntrl_data['allow'] else False
        for key, value in self._cntrl_data["ratelimit"].items():
            if key == 'cnt':
                self._cntrl_data["ratelimit"][key] = int(value)

        if zm_monitor is not None:
            self._monitor = ZmMonitor(ad_parent.zm_api, zm_monitor, mfnc, self._cntrl_data, logger)
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
            self._ad.set_state(self._gate, state="on")

    def set_squelch(self):
        if self.is_notify_enabled() and self._window_timer is not None:
            self.log("Sensor {} squelched at event cnt: {}".format(self.name, self._event_cnt))
            self._ad.set_state(self._gate, state="off")
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
    zm_api: Zmes
    img_types = ['jpg', 'gif', 'png', 'tif', 'svg', 'jpeg']
    ts_fmt = '%a %I:%M %p'
    log_header = 'ZM ES Handler'

    def init(self):
        self._version = __version__
        self.notify_list = []
        self.zm_options = {
            'apiurl': None,
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
        self.clean_files_in_local_cache()
        self.cache_file_cnt = 0

    @staticmethod
    def version():
        return __version__

    def initialize(self):
        """
        initialize() function which will be called at startup and reload
        """
        self.log('{} initializing'.format(self.log_header))
        self.init()
        try:
            self.zm_options['apiurl'] = self.args["zm_url"] + self.args["zmapi_loc"]
            self.zm_options['user'] = self.args["zm_user"]
            self.zm_options['password'] = self.args["zm_pw"]
            self.img_width = self.args["img_width"]
            self.img_cache_dir = self.args["img_cache_dir"]
            self.img_frame_type = self.args["img_frame_type"]
            self.txt_blk_list = self.args["txt_blk_list"]
            for notify_id in self.args["notify"]:
                if notify_id is list:
                    self.notify_list.extend(notify_id)
                else:
                    self.notify_list.append(notify_id)
            if not self.args["zmapi_use_token"]:
                self.zm_options['token'] = False
        except KeyError:
            self.log("Missing arguments in yaml setup file")
            raise
        # lets init the API
        if self.zm_api is None:
            try:
                self.zm_api = Zmes(options=self.zm_options)
            except Exception as e:
                self.error('Error: {}'.format(str(e)))
                self.error(traceback.format_exc())
                raise
        # sensors is a dict of sensorid with associated notify gate
        for sensor in self.args["sensors"]:
            new_sensor = "sensor." + sensor
            self.log("adding listener for sensor {}".format(new_sensor))
            self.sensors[new_sensor] = HASensor(self, new_sensor, self.args["sensors"][sensor], self.logger)
            self.listen_state(self.handle_state_change, new_sensor)

        # at this point we should authenticated with zoneminder
        self.log('Zoneminder ES Handler init completed')

    def clean_files_in_local_cache(self):
        exp = os.path.join(self.img_cache_dir, "*.*")
        file_list = glob.glob(exp)
        for filePath in file_list:
            try:
                os.remove(filePath)
            except:
                pass

    def write_file_to_local_cache(self, img_data, filepath):
        """
        Save image data to specfied file
        :param img_data: raw data from http get request
        :param filepath: complete local file path
        """
        with iopen(filepath, 'wb') as file:
            file.write(img_data.content)
            self.cache_file_cnt += 1

    def gen_file_path(self, fname, ftype):
        filepath = os.path.join(self.img_cache_dir, fname + '.' + ftype)
        cntr = 0
        while os.path.exists(filepath):
            nfilenam = '{}_{}'.format(fname, cntr)
            filepath = os.path.join(self.img_cache_dir, nfilenam + '.' + ftype)
            cntr += 1
        return filepath

    def get_image_file_type(self, img_data, suffix_list=img_types):
        """
        Check that the reported file type is something we know how to process
        :param img_data: raw image data from http get request
        :param suffix_list: list of supported file types
        :return: string indicating filetype or None if not supported
        """
        if img_data is not None:
            filetype = img_data.headers['Content-Type'].split('/')[1]
            self.log("ZM ES Handler: img file type: {}".format(filetype))
            return filetype
        return None

    def is_image_file_type_supported(self, filetype):
        return filetype in self.img_types

    def save_image_to_file(self, event_id, fid, img_data, filetype):
        filename = '{}-ev{}'.format(fid, event_id)
        filepath = self.gen_file_path(filename, filetype)
        if self.is_image_file_type_supported(filetype):
            self.log('Writing image file {}'.format(filepath))
            self.write_file_to_local_cache(img_data, filepath)
            return filepath
        else:
            self.error("{}: image filetype {} NOT SUPPORTED".format(self.log_header, filetype))
            self.log('Writing invalid image file {}'.format(filepath))
            self.write_file_to_local_cache(img_data, filepath)
            return None

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
        zm_sensor.process_event()
        if self.get_state(zm_sensor.ha_gate) == 'on':
            self.log('processing state change for entity: {}'.format(entity))
            # gate is on, so proceed with notifications
            timestamp = dt.now().strftime(self.__class__.ts_fmt)
            camera, detail = new.split(':(')
            event_id, sub_detail = detail.split(') ')
            frame, txt_body = sub_detail.split('] ')
            txt_body = self.clean_text_msg(txt_body, self.txt_blk_list)
            msg_title = '{} Camera alert @{}\n'.format(camera, timestamp)
            fid = frame[1:]
            zm_event = zm_sensor.monitor().find_event(event_id)
            if zm_event is not None:
                self.log("found ZM Event ({}) for id {}".format(zm_event.name(), zm_event.id()))
            else:
                self.error("failed to find ZM Event for id {}".format(event_id))
            # attempt to pull the image based on the configured frame type
            # but if not available, pull the type indicated in the name/msg field
            ftl = [self.img_frame_type, fid]
            ft_min_set = [i for n, i in enumerate(ftl) if i not in ftl[:n]]
            raw_img_data = None
            img_file_type = None
            attempt = 1
            for entry in ft_min_set:
                self.log("Attempt #({}): pull image file with fid: {}".format(attempt, entry))
                for retry in range(0, 1):
                    raw_img_data = self.zm_api.get_event_image_data(event_id,
                                                                    fid=self.get_fid(entry), px_width=self.img_width)
                    img_file_type = self.get_image_file_type(raw_img_data)
                    if self.is_image_file_type_supported(img_file_type):
                        break
                    else:
                        self.save_image_to_file(event_id, self.get_fid(fid), raw_img_data, filetype=img_file_type)
                if raw_img_data is not None and self.is_image_file_type_supported(img_file_type):
                    self.log("Successfully pulled Zoneminder image for event id:{} camera: {} msg: {} "
                             "frame type [{}]".format(event_id, camera, txt_body, entry))
                    break
                attempt += 1
            if self.is_image_file_type_supported(img_file_type):
                img_file_uri = self.save_image_to_file(event_id, self.get_fid(fid), raw_img_data,
                                                       filetype=img_file_type)
                if img_file_uri is not None:
                    for notifier in self.notify_list:
                        notify_path = 'notify/' + notifier
                        self.log("ZM ES Handler: sending text to {} for event: {}".format(notify_path, event_id))
                        self.call_service(notify_path, message=txt_body, title=msg_title,
                                          data={'image_file': img_file_uri})
                else:
                    self.log("ZM ES Handler: failed to save image file for event: {}".format(event_id))
            else:
                self.log("Failed to pull Zoneminder image for event id:{} camera: {} msg: {}".format(event_id, camera,
                                                                                                     txt_body))
        else:
            self.log("ZM ES Handler: notify gate is turned off for entity: {}".format(entity))
        return
