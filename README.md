# ZoneMinder Notify App

Appdaemon App to run under Home Assistant Appdaemon

This App Registers for state change notification on the sensors 
specified via the zmnotify.yaml config file.
These sensors proxy Zoneminder MQTT topics as generated from 
Zoneminder ES with the MQTT option enabled. Info from this is 
massaged into a text message and then attaches an image frame. 
Every Zoneminder event has an integer ID. The image frame is
pulled from the Zoneminder server using the integer event id.

**NOTE:** This is a work in progress.

The zmnotify.yaml file provides the configuration parameters
for this app. This file is read by Appdaemon and passed to 
this app via the initialize() method as defined by the Appdaemon
API.