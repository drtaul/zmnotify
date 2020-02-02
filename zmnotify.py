'''
Appdaemon App to run under Home Assistant Appdaemon

Registers for state change notification on the specified sensors.
These sensors are MQTT data as generated from Zoneminder ES with the
MQTT option enabled. Info from this is massaged into a text message
and then attaches a image frame for the Zoneminder Event Id.

**NOTE:** This is a work in progress.
'''
import os
import traceback
import appdaemon.plugins.hass.hassapi as hass
import pyzm.api as zmAPI
import requests
from io import open as iopen
from datetime import datetime as dt


class Zmes(zmAPI.ZMApi):
    '''
    Zoneminder Event Server Proxy
    This is a placeholder until the ZMApi class and pyzm helpers
    are extended to support the functions provided by this class.
    '''

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


# noinspection PyAttributeOutsideInit
class ZmEventNotifier(hass.Hass):
    '''
    Appdaemon class.
    '''
    zm_api: Zmes
    img_types = ['jpg', 'gif', 'png', 'tif', 'svg', 'jpeg']
    ts_fmt = '%a %I:%M %p'

    def init(self):
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

    def initialize(self):
        """
        initialize() function which will be called at startup and reload
        """
        self.log('Zoneminder ES Handler initializing')
        self.init()
        try:
            self.zm_options['apiurl'] = self.args["zm_url"] + self.args["zmapi_loc"]
            self.zm_options['user'] = self.args["zm_user"]
            self.zm_options['password'] = self.args["zm_pw"]
            self.img_width = self.args["img_width"]
            self.img_cache_dir = self.args["img_cache_dir"]
            self.img_frame_type = self.args["img_frame_type"]
            self.txt_blk_list = self.args["txt_blk_list"]
            # sensors is a dict of sensorid with associated notify gate
            for sensor in self.args["sensors"]:
                new_sensor = "sensor." + sensor
                self.log("adding listener for sensor {}".format(new_sensor))
                self.sensors[new_sensor] = self.args["sensors"][sensor]
                self.listen_state(self.handle_state_change, new_sensor)
            for notify_id in self.args["notify"]:
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
        # at this point we should authenticated with zoneminder
        self.log('Zoneminder ES Handler init completed')

    def check_image_file_type(self, img_data, suffix_list=img_types):
        """
        Check that the reported file type is something we know how to process
        :param img_data: raw image data from http get request
        :param suffix_list: list of supported file types
        :return: string indicating filetype or None if not supported
        """
        filetype = img_data.headers['Content-Type'].split('/')[1]
        self.log("ZM ES Handler img file type: {}".format(filetype))
        if filetype in suffix_list:
            return filetype
        else:
            return None

    def save_image_to_file(self, event_id, fid, img_data, filetype, suffix_list=img_types):
        if filetype in suffix_list:
            filename = '{}-ev{}.{}'.format(fid, event_id, filetype)
            filepath = os.path.join(self.img_cache_dir, filename)
            cntr = 0
            while os.path.exists(filepath):
                nfilenam = '{}-ev{}_{}.{}'.format(fid, event_id, cntr, filetype)
                filepath = os.path.join(self.img_cache_dir, nfilenam)
                cntr += 1
            self.log('Writing image file {}'.format(filepath))
            with iopen(filepath, 'wb') as file:
                file.write(img_data.content)
            return filepath
        else:
            self.error("ZM ES Handler: filetype {} not in supported files, aborting".format(filetype))
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
        if self.get_state(self.sensors[entity]) == 'on':
            self.log('Zoneminder ES Handler processing state change for entity: {}'.format(entity))
            # gate is on, so proceed with notifications
            timestamp = dt.now().strftime(self.__class__.ts_fmt)
            camera, detail = new.split(':(')
            event_id, sub_detail = detail.split(') ')
            frame, txt_body = sub_detail.split('] ')
            txt_body = self.clean_text_msg(txt_body, self.txt_blk_list)
            msg_title = '{} Camera alert @{}\n'.format(camera, timestamp)
            fid = frame[1:]
            # attempt to pull the image based on the configured frame type
            # but if not available, pull the type indicated in the name/msg field
            ft_set = set([self.img_frame_type, fid])
            raw_img_data = None
            img_file_type = None
            for entry in ft_set:
                raw_img_data = self.zm_api.get_event_image_data(event_id,
                                                                fid=self.get_fid(entry), px_width=self.img_width)
                img_file_type = self.check_image_file_type(raw_img_data)
                if raw_img_data is not None and img_file_type is not None:
                    self.log("Successfully pulled Zoneminder image for event id:{} camera: {} msg: {} "
                             "frame type [{}]".format(event_id, camera, txt_body, entry))
                    break
            if img_file_type is not None:
                img_file_uri = self.save_image_to_file(event_id, fid, raw_img_data, filetype=img_file_type)
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
