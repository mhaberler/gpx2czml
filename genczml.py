#!/usr/bin/env python3
import sys
import re
import json
import argparse
import logging
from itertools import count
import random
from datetime import datetime, timedelta, MINYEAR, MAXYEAR
import pathlib
import pytz
from yattag import Doc
import gpxpy
from aerofiles.igc.reader import Reader
from pykml import parser
import dateutil.parser
import csv
import numpy as np

from transforms3d.quaternions import qconjugate, qmult, qisunit
from transformations import (
    rotation_matrix,
    quaternion_from_matrix,
    quaternion_multiply,
    quaternion_conjugate,
)
from czml3 import Document, Packet, Preamble
from czml3.enums import (
    HeightReferences,
    HorizontalOrigins,
    InterpolationAlgorithms,
    LabelStyles,
    ReferenceFrames,
    VerticalOrigins,
)
from czml3.properties import (
    Billboard,
    Clock,
    Color,
    Label,
    Point,
    Material,
    Model,
    Path,
    Position,
    Orientation,
    PositionList,
    Polyline,
    SolidColorMaterial,
    PolylineOutlineMaterial,
    PolylineDashMaterial,
    PolylineMaterial,
    ViewFrom,
)
from czml3.types import (
    IntervalValue,
    Sequence,
    TimeInterval,
    format_datetime_like,
    Cartesian3Value,
)

from cesiumNEDtoFixedFrame import northEastDownToFixedFrame
from orient import hpr2Quaternion, corrQuaternion

default_delta = 3  # m
volume_cutoff = 6  # dB under peakvolume
TRACK_GPX = 1
TRACK_IGC = 2
TRAJ_WINDY_GPX = 3
TRAJ_WINDY_KML = 4
TRAJ_METEOBLUE = 5
TRACK_SENSORLOG = 6

DEFAULT_MODEL = "https://static.mah.priv.at/cors/OE-SOX-flames.glb"
FUDGE = 2

# viewFromPos=[45, 400, 200]
viewFromPos = [135, 100, 50]


mbcolors = {
    "surface": ((0, 0, 204, 255), (0, 204, 255, 255)),
    "975mb": ((51, 153, 51, 255), (0, 204, 0, 255)),
    "950mb": ((255, 51, 0, 255), (254, 148, 153, 255)),
    "900mb": ((51, 153, 102, 255), (102, 102, 255, 255)),
    "850mb": ((255, 0, 0, 255), (133, 68, 255, 255)),
    "800mb": ((255, 144, 27, 255), (76, 212, 255, 255)),
    "750mb": ((255, 51, 153, 255), (112, 238, 196, 255)),
    "700mb": ((153, 51, 0, 255), (238, 241, 0, 255)),
    "650mb": ((153, 51, 255, 255), (102, 255, 51, 255)),
    "600mb": ((51, 204, 204, 255), (204, 0, 102, 255)),
    "550mb": ((255, 153, 0, 255), (0, 204, 102, 255)),
    "500mb": ((0, 0, 204, 255), (0, 204, 255, 255)),
    "450mb": ((51, 153, 51, 255), (0, 204, 0, 255)),
    "400mb": ((255, 51, 0, 255), (254, 148, 153, 255)),
    "350mb": ((51, 153, 102, 255), (102, 102, 255, 255)),
    "300mb": ((255, 0, 0, 255), (133, 68, 255, 255)),
    "250mb": ((255, 144, 27, 255), (76, 212, 255, 255)),
    "200mb": ((255, 51, 153, 255), (112, 238, 196, 255)),
    "150mb": ((153, 51, 0, 255), (238, 241, 0, 255)),
}

sensorlog_keys = [
    "accelerometerAccelerationX",
    "accelerometerAccelerationY",
    "accelerometerAccelerationZ",
    "accelerometerTimestamp_sinceReboot",
    "activity",
    "activityActivityConfidence",
    "activityActivityStartDate",
    "activityTimestamp_sinceReboot",
    "altimeterPressure",
    "altimeterRelativeAltitude",
    "altimeterReset",
    "altimeterTimestamp_sinceReboot",
    "avAudioRecorderAveragePower",
    "avAudioRecorderPeakPower",
    "batteryLevel",
    "batteryState",
    "deviceID",
    "deviceOrientation",
    "gyroRotationX",
    "gyroRotationY",
    "gyroRotationZ",
    "gyroTimestamp_sinceReboot",
    "identifierForVendor",
    "IP_en0",
    "IP_pdp_ip0",
    "label",
    "locationAltitude",
    "locationCourse",
    "locationFloor",
    "locationHeadingAccuracy",
    "locationHeadingTimestamp_since1970",
    "locationHeadingX",
    "locationHeadingY",
    "locationHeadingZ",
    "locationHorizontalAccuracy",
    "locationLatitude",
    "locationLongitude",
    "locationMagneticHeading",
    "locationSpeed",
    "locationTimestamp_since1970",
    "locationTrueHeading",
    "locationVerticalAccuracy",
    "loggingTime",
    "logSampleNr",
    "magnetometerTimestamp_sinceReboot",
    "magnetometerX",
    "magnetometerY",
    "magnetometerZ",
    "motionAttitudeReferenceFrame",
    "motionGravityX",
    "motionGravityY",
    "motionGravityZ",
    "motionMagneticFieldCalibrationAccuracy",
    "motionMagneticFieldX",
    "motionMagneticFieldY",
    "motionMagneticFieldZ",
    "motionPitch",
    "motionQuaternionW",
    "motionQuaternionX",
    "motionQuaternionY",
    "motionQuaternionZ",
    "motionRoll",
    "motionRotationRateX",
    "motionRotationRateY",
    "motionRotationRateZ",
    "motionTimestamp_sinceReboot",
    "motionUserAccelerationX",
    "motionUserAccelerationY",
    "motionUserAccelerationZ",
    "motionYaw",
]

required = set(
    {
        "loggingTime",
        "locationLongitude",
        "locationLongitude",
        "locationAltitude",
        "locationHorizontalAccuracy",
        "locationVerticalAccuracy",
    }
)

burner_required = set({"loggingTime", "avAudioRecorderPeakPower"})

MIN_HDOP = 10
MIN_VDOP = 5


class PathSet:
    _serial = count(0)
    _labels = count(0)

    def __init__(
        self,
        gpx=None,  # enclosing GPX file, parsed
        kml=None,  # KML traj
        track=None,  # current track
        # trajectory=None,  # for simulated flights
        sensorlog=None,
        typus=TRACK_GPX,
        filename=None,
    ):

        self.serial = next(self._serial)
        self.gpx = gpx
        self.kml = kml
        self.track = track
        self.typus = typus
        self.sensorlog = sensorlog
        self.filename = filename

        if self.typus == TRACK_SENSORLOG:
            self.first_valid = -1
            self.last_valid = -1
            i = -1
            for sample in self.sensorlog:
                i += 1
                if not required <= set(sample):
                    # logging.debug(f"skipping sample")
                    continue
                if float(sample["locationHorizontalAccuracy"]) > MIN_HDOP:
                    continue
                if float(sample["locationVerticalAccuracy"]) > MIN_VDOP:
                    continue
                # a valid sample
                if self.first_valid < 0:
                    self.first_valid = i
                if self.last_valid < i:
                    self.last_valid = i
            logging.debug(
                f"{self.filename} valid: {self.first_valid}..{self.last_valid} of {i}"
            )

    def starttime(self, skip=0):
        if self.typus == TRACK_SENSORLOG:
            datestring = self.sensorlog[self.first_valid]["loggingTime"]
            return dateutil.parser.parse(datestring) + timedelta(seconds=skip)
        return self.track.get_time_bounds().start_time + timedelta(seconds=skip)

    def stoptime(self, trim=0):
        if self.typus == TRACK_SENSORLOG:
            datestring = self.sensorlog[self.last_valid]["loggingTime"]
            return dateutil.parser.parse(datestring) - timedelta(seconds=trim)
        return self.track.get_time_bounds().end_time - timedelta(seconds=trim)

    def availability(self, skip=0, trim=0):
        return TimeInterval(
            start=self.starttime(skip=skip), end=self.stoptime(trim=trim)
        )

    def duration(self, skip=0, trim=0):
        return (self.stoptime(trim=trim) - self.starttime(skip=skip)).total_seconds()

    def data(
        self,
        zcorrect=0,
        timestamps=True,
    ):
        results = []

        if self.typus == TRACK_SENSORLOG:
            start = self.starttime()

            for sample in self.sensorlog[self.first_valid : self.last_valid + 1]:
                if not required <= set(sample):
                    # logging.debug(f"skipping sample")
                    continue
                if float(sample["locationHorizontalAccuracy"]) > MIN_HDOP:
                    continue
                if float(sample["locationVerticalAccuracy"]) > MIN_VDOP:
                    continue

                sampleTime = dateutil.parser.parse(sample["loggingTime"])
                timetag = timedelta.total_seconds(sampleTime - start)

                # logging.debug(f"use {timetag=} {sampleTime=}")
                results.extend(
                    [
                        timetag,
                        float(sample["locationLongitude"]),
                        float(sample["locationLatitude"]),
                        float(sample["locationAltitude"]) + zcorrect,
                    ]
                )
            return results

        if self.typus == TRACK_GPX:

            start = self.starttime()

            for (point, segment, point_no) in self.track.walk():
                if timestamps:
                    results.append(timedelta.total_seconds(point.time - start))
                results.extend(
                    [point.longitude, point.latitude, point.elevation + zcorrect]
                )
            return results

        raise Exception

    def hpr_default(self, args, ts, secs, duration, lat, lon, alt):

        if secs < 30:
            return (-120, 0, 90)

        if secs > 60:
            return (0, 0, 0)
        if secs > 90:
            return (0, 0, 0)

        if duration - secs < 60:
            return (0, 0, 1)

        if duration - secs < 20:
            return (0, 0, 90)

        return (-90, 0, 0)

    def burner_intervals(self, args):
        if self.typus == TRACK_SENSORLOG:
            start = self.starttime()
            peakvolumes = []
            maxvolume = -1
            was_burning = False
            cart0 = [0.1, 0.1, 0.1]
            cart1 = [1, 1, 1]

            samples = []
            samples.append({"interval": self.availability(), "cartesian": cart0})

            # determine epeak volume
            for sample in self.sensorlog:

                if not burner_required <= set(sample):
                    logging.debug(f"skipping sample")
                    continue
                peak = float(sample["avAudioRecorderPeakPower"])
                if peak > maxvolume:
                    maxvolume = peak
            cutoff = maxvolume - volume_cutoff
            logging.debug(f"sensorlog: audio {maxvolume=} {cutoff=} dB")

            for sample in self.sensorlog:
                if not burner_required <= set(sample):
                    logging.debug(f"skipping sample")
                    continue

                sampleTime = dateutil.parser.parse(sample["loggingTime"])
                # timetag = timedelta.total_seconds(sampleTime - start)
                peak = float(sample["avAudioRecorderPeakPower"])

                burning = peak > cutoff
                if burning ^ was_burning:  # different
                    if burning:
                        burnstart = sampleTime
                    else:
                        samples.append(
                            {
                                "interval": format_datetime_like(burnstart)
                                + "/"
                                + format_datetime_like(sampleTime),
                                "cartesian": cart1,
                            }
                        )
                was_burning = burning
        return samples

    def orient(self, args, hprfunc=hpr_default):

        if self.typus == TRACK_SENSORLOG:

            start = self.starttime()
            results = []

            for sample in self.sensorlog[self.first_valid : self.last_valid + 1]:

                if not required <= set(sample):
                    # logging.debug(f"skipping sample")
                    continue

                sampleTime = dateutil.parser.parse(sample["loggingTime"])
                timetag = timedelta.total_seconds(sampleTime - start)

                if args.gyro:
                    a = hpr2Quaternion(
                        float(sample["locationLatitude"]),
                        float(sample["locationLongitude"]),
                        float(sample["locationAltitude"]),
                        np.degrees(float(sample["motionYaw"])),
                        np.degrees(float(sample["motionPitch"])),
                        np.degrees(float(sample["motionRoll"])),
                    )
                else:
                    a = hpr2Quaternion(
                        float(sample["locationLatitude"]),
                        float(sample["locationLongitude"]),
                        float(sample["locationAltitude"]),
                        float(sample["locationTrueHeading"]),
                        0,
                        0,
                    )

                # [Time, X, Y, Z, W, Time, X, Y, Z, W, ...]
                # https://github.com/AnalyticalGraphicsInc/czml-writer/wiki/UnitQuaternionValue
                results.extend([timetag, a[0], a[1], a[2], a[3]])

            return results

        # if self.typus == TRACK_GPX:
        prev_hpr = (-1, -1, -1)
        results = []
        start = self.starttime()
        duration = self.duration()
        for (point, segment, point_no) in self.track.walk():
            timetag = timedelta.total_seconds(point.time - start)

            if not args.fake_orient:
                hpr = (0, 0, 0)
            else:
                hpr = hprfunc(
                    self,
                    args,
                    point.time,
                    timetag,
                    duration,
                    point.latitude,
                    point.longitude,
                    point.elevation + args.delta_h,
                )
            if hpr == prev_hpr:
                continue
            prev_hpr = hpr
            heading, pitch, roll = hpr

            o = hpr2Quaternion(
                point.latitude,
                point.longitude,
                point.elevation + args.delta_h,
                heading,
                pitch,
                roll,
            )
            results.append(timetag)
            results.extend(o)
        return results

    def dumps(self, indent=4, **kwargs):
        d = self.data(**kwargs)
        return json.dumps(d, indent=indent)

    def gen_traj_windy(self, args, packets, **kwargs):
        rgb = self.ext["{http://www.topografix.com/GPX/gpx_style/0/2}color"]
        opacity = int(
            float(self.ext["{http://www.topografix.com/GPX/gpx_style/0/2}opacity"])
            * 255
        )
        color = list(int(rgb[i : i + 2], 16) for i in (0, 2, 4))
        color.append(opacity)

        markup, tag, text, line = Doc().ttl()

        with tag("ul", id="traj-data"):
            for k, v in self.ext.items():
                line("li", k + ": " + v)
            line("li", "filename: " + self.filename)
        sc = SolidColorMaterial(color=Color(rgba=color))
        m1 = PolylineMaterial(solidColor=sc)

        p1 = PositionList(cartographicDegrees=self.data(timestamps=False))

        on_ground = self.ext["level"] == "surface"
        pl = Polyline(
            show=True, width=2, clampToGround=on_ground, material=m1, positions=p1
        )
        p = Packet(
            id=f"traj{self.serial}",
            name="fixme windy gpx traj",
            polyline=pl,
            description=markup.getvalue(),
        )
        packets.append(p)

    def gen_traj_windy_kml(self, args, packets, **kwargs):
        for f in self.kml.Document.Folder:
            # logging.debug(f"Top folder: {f.name=}")

            for e in f.Folder:  # Through all Placemark
                logging.debug(f"//Folder {e.name=} {e.xpath('@id')=} {e.description=}")
                tid = f"{e.xpath('@id')[0]}"
                model = re.search(r"Model:([^\<]+)", str(e.description)).group(1)

                for p in e.Placemark:
                    logging.debug(
                        f"--Placemark: {p.name=} { p.description=} {p.MultiGeometry.LineString.coordinates=}"
                    )
                    # logging.debug(f"pm style: {p.Style.LineStyle.color=} {p.Style.LineStyle.width=}")
                    pl = [
                        float(f)
                        for f in list(
                            filter(
                                None,
                                re.split(
                                    "[, \-!?:]+",
                                    str(p.MultiGeometry.LineString.coordinates),
                                ),
                            )
                        )
                    ]

                    sc = SolidColorMaterial(
                        color=Color().from_str("#" + str(p.Style.LineStyle.color))
                    )
                    m1 = PolylineMaterial(solidColor=sc)
                    p1 = PositionList(cartographicDegrees=pl)
                    on_ground = p.name == "surface"
                    pl = Polyline(
                        show=True,
                        width=2,
                        clampToGround=on_ground,
                        material=m1,
                        positions=p1,
                    )
                    p = Packet(
                        id=f"traj{next(self._serial)}",
                        name=model + " " + p.name + ", " + tid,
                        polyline=pl
                        # ,
                        # description=markup.getvalue()
                    )
                    packets.append(p)

                for g in e.Folder:
                    logging.debug(f"sf Folder: {g.name=} {g.xpath('@id')=}")
                    fid = str(g.xpath("@id")[0])
                    for h in g.Placemark:
                        logging.debug(
                            f"sf Placemark: {h.description=} {h.Point.coordinates=}"
                        )
                        coords = [
                            float(f)
                            for f in list(
                                filter(None, re.split("[,]", str(h.Point.coordinates)))
                            )
                        ]

                        logging.debug(
                            f"sf pm style: {h.Style.IconStyle.color=} {h.Style.LabelStyle.color=}"
                        )

                        lb = Packet(
                            id=f"trajpt{next(self._serial)}",
                            name=f"{fid}",
                            description=str(h.description),
                            point=Point(
                                color=Color().from_str(
                                    "#" + str(h.Style.IconStyle.color)
                                ),
                                outlineColor=Color.from_list([0, 0, 0]),
                                outlineWidth=1,
                                pixelSize=5,
                                # heightReference=href
                            ),
                            position=Position(cartographicDegrees=coords),
                        )

                        packets.append(lb)

    def gen_traj_meteoblue(self, args, packets, dashed=True, **kwargs):

        if dashed:
            c = mbcolors[self.track.name][0]
            gc = mbcolors[self.track.name][1]
            pd = PolylineDashMaterial(
                color=Color(rgba=c),
                gapColor=Color(rgba=gc),
                dashLength=32,
                dashPattern=255,
            )
            m1 = PolylineMaterial(polylineDash=pd)
        else:
            color = mbcolors[self.track.name][0]
            sc = SolidColorMaterial(color=Color(rgba=color))
            m1 = PolylineMaterial(solidColor=sc)

        on_ground = self.track.name == "surface"

        markup, tag, text, line = Doc().ttl()
        with tag("ul", id="grocery-list"):
            if self.gpx.name:
                line("li", "Name: %s" % self.gpx.name)
            if self.gpx.description:
                line("li", "description: %s" % self.gpx.description)
            if self.gpx.author_name:
                line("li", "Author: %s" % self.gpx.author_name)
            if self.gpx.author_email:
                line("li", "Email: %s" % self.gpx.author_email)
            if self.gpx.time:
                line("li", "Time: %s" % self.gpx.time)
            line("li", "filename: " + self.filename)

        positionlist = PositionList(cartographicDegrees=self.data(timestamps=False))

        pl = Polyline(
            show=True,
            width=2,
            zIndex=2,
            clampToGround=on_ground,
            material=m1,
            positions=positionlist,
        )
        p = Packet(
            id=f"traj{self.serial}",
            name="fixme windy meteoblue traj",
            polyline=pl,
            description=markup.getvalue(),
        )
        packets.append(p)

    def gen_markers_meteoblue(self, args, packets, **kwargs):
        for w in self.gpx.waypoints:
            logging.debug(f"  Waypoint: {w}")
            # lb = czml3.Label(text=, show=True)
            lb = Packet(
                id="trajlabel%s" % next(self._labels),
                name="Waypoint - Meteoblue Trajectory",
                label=Label(
                    horizontalOrigin=HorizontalOrigins.LEFT,
                    show=True,
                    font="11pt Lucida Console",
                    style=LabelStyles.FILL_AND_OUTLINE,
                    outlineWidth=2,
                    text=f"{w.name}",
                    verticalOrigin=VerticalOrigins.CENTER,
                    fillColor=Color.from_list([255, 255, 255]),
                    outlineColor=Color.from_list([0, 0, 0]),
                ),
                position=Position(
                    cartographicDegrees=[w.longitude, w.latitude, w.elevation]
                ),
            )
            packets.append(lb)
            # lb.scale=0.5
            # packet2.label=lb
        for tr in self.gpx.tracks:
            # logging.debug(f" {tr.name=}")
            tpcolor = mbcolors[tr.name][0]
            for seg in tr.segments:
                # logging.debug(f" {seg=}")
                for tp in seg.points:
                    # name time latitude longitude elevation
                    # logging.debug(f" Trackpoint: {dir(tp)}")
                    href = HeightReferences.NONE
                    h = tp.elevation
                    if tr.name == "surface":
                        href = HeightReferences.RELATIVE_TO_GROUND
                        h = 250
                    lb = Packet(
                        id="trajpt%s" % next(self._labels),
                        name=tp.name + "@" + str(tp.time),
                        point=Point(
                            color=Color(rgba=mbcolors[tr.name][0]),
                            outlineColor=Color(rgba=mbcolors[tr.name][1]),
                            outlineWidth=1,
                            pixelSize=5,
                            heightReference=href,
                        ),
                        position=Position(
                            cartographicDegrees=[tp.longitude, tp.latitude, h]
                        ),
                    )
                    packets.append(lb)

    def gen_markers_windy(self, args, packets, **kwargs):
        rgb = self.ext["{http://www.topografix.com/GPX/gpx_style/0/2}color"]
        opacity = int(
            float(self.ext["{http://www.topografix.com/GPX/gpx_style/0/2}opacity"])
            * 255
        )
        color = list(int(rgb[i : i + 2], 16) for i in (0, 2, 4))
        color.append(opacity)
        logging.debug(f"-- {self.track=}")

        for seg in self.track.segments:
            # logging.debug(f" {seg=}")
            for tp in seg.points:
                # name time latitude longitude elevation
                # logging.debug(f" Trackpoint: {dir(tp)}")
                href = HeightReferences.NONE
                h = tp.elevation
                if self.track.name == "surface":
                    href = HeightReferences.RELATIVE_TO_GROUND
                    h = 0
                lb = Packet(
                    id="trajpt%s" % next(self._labels),
                    name=tp.name + "@" + str(tp.time),
                    point=Point(
                        color=Color(rgba=color),
                        outlineColor=Color.from_list([0, 0, 0]),
                        outlineWidth=1,
                        pixelSize=5,
                        heightReference=href,
                    ),
                    position=Position(
                        cartographicDegrees=[tp.longitude, tp.latitude, h]
                    ),
                )
                packets.append(lb)

    def gen_track_gpx(self, args, packets, vehicle=None, genlabel=False, **kwargs):
        logging.debug(f"GPX track {self.filename} {args=}")
        lb = None
        if genlabel:
            lb = Label(
                text="fixme gen_track",
                show=True,
                scale=0.5,
                pixelOffset={"cartesian2": [50, -30]},
            )

        position = Position(
            interpolationDegree=args.degree,
            interpolationAlgorithm=args.algo,
            epoch=self.starttime(),
            cartographicDegrees=self.data(zcorrect=args.delta_h),
        )

        red = Color(rgba=[255, 0, 0, 64])
        grn = Color(rgba=[0, 255, 0, 64])
        po = PolylineOutlineMaterial(color=red, outlineColor=grn, outlineWidth=4)
        path = Path(
            material=Material(polylineOutline=po),
            width=6,
            leadTime=0,
            trailTime=100000,
            resolution=5,
        )

        # example:
        # "nodeTransformations": {
        #         "Burner1": {
        #           "scale": [
        #             {
        #               "interval": "2019-10-23T06:14:26Z/2019-10-23T06:14:30Z",
        #               "cartesian": [
        #                 0.2,
        #                 0.2,
        #                   0.2
        #               ]
        #             },
        #             {
        #               "interval": "2019-10-23T06:14:43Z/2019-10-23T06:14:46Z",
        #               "cartesian": [
        #                 1.2,
        #                 1.2,
        #                 1.2
        #               ]
        #             }
        #           ]
        #         }
        #     }

        nt = {
            "Burner1": {"scale": self.simulate_burner_transformations(args)},
            "Burner2": {"scale": self.simulate_burner_transformations(args)},
        }
        vehicle = Model(
            gltf=args.model_uri, scale=1.0, minimumPixelSize=64, nodeTransformations=nt
        )

        kwargs = dict()
        properties = dict()

        options = {
            "velocity_orient": args.velocity_orient,
        }

        properties = {**properties, **options}
        if args.fake_sensors:
            properties = {**properties, **self.simulate_sensors(args)}

        p = Packet(
            id=f"track{self.serial}",
            name=args.docname,
            # viewFrom=45,800,300&lookAt=track0
            viewFrom=ViewFrom(cartesian=Cartesian3Value(values=viewFromPos)),
            description=args.doccomment,
            position=position,
            # orientation=Orientation(unitQuaternion=[0, 0, 0, 1]),
            orientation=Orientation(
                interpolationDegree=1,
                interpolationAlgorithm=args.algo,
                epoch=self.starttime(),
                unitQuaternion=self.orient(args),
            ),
            label=lb,
            path=path,
            model=vehicle,
            availability=self.availability(),
            properties=properties,
            **kwargs,
        )
        packets.append(p)

    def gen_track_sensorlog(
        self, args, packets, vehicle=None, genlabel=False, **kwargs
    ):
        logging.debug(f"sensorlog track {self.filename} {args=}")
        lb = None
        if genlabel:
            lb = Label(
                text="fixme  gen_track_sensorlog",
                show=True,
                scale=0.5,
                pixelOffset={"cartesian2": [50, -30]},
            )

        position = Position(
            interpolationDegree=args.degree,
            interpolationAlgorithm=args.algo,
            epoch=self.starttime(),
            cartographicDegrees=self.data(zcorrect=args.delta_h),
        )

        red = Color(rgba=[255, 0, 0, 64])
        grn = Color(rgba=[0, 255, 0, 64])
        po = PolylineOutlineMaterial(color=red, outlineColor=grn, outlineWidth=4)
        path = Path(
            material=Material(polylineOutline=po),
            width=6,
            leadTime=0,
            trailTime=100000,
            resolution=5,
        )

        nt = {
            "Burner1": {"scale": self.burner_intervals(args)},
            "Burner2": {"scale": self.burner_intervals(args)},
        }
        vehicle = Model(
            gltf=args.model_uri, scale=1.0, minimumPixelSize=64, nodeTransformations=nt
        )
        kwargs = dict()
        properties = dict()

        options = {
            "velocity_orient": args.velocity_orient,
        }

        properties = {**properties, **options}
        if args.fake_sensors:
            properties = {**properties, **self.simulate_sensors(args)}

        p = Packet(
            id=f"track{self.serial}",
            name=args.docname,
            # viewFrom=45,800,300&lookAt=track0
            viewFrom=ViewFrom(cartesian=Cartesian3Value(values=viewFromPos)),
            description=args.doccomment,
            position=position,
            orientation=Orientation(
                interpolationDegree=args.degree,
                interpolationAlgorithm=args.algo,
                epoch=self.starttime(),
                unitQuaternion=self.orient(args),
            ),
            label=lb,
            path=path,
            model=vehicle,
            availability=self.availability(),
            properties=properties,
            **kwargs,
        )
        packets.append(p)

    def generate(self, args, packets, **kwargs):
        if self.typus == TRAJ_WINDY_GPX:
            self.gen_traj_windy(args, packets, **kwargs)
            self.gen_markers_windy(args, packets, **kwargs)

        if self.typus == TRAJ_WINDY_KML:
            self.gen_traj_windy_kml(args, packets, **kwargs)

        if self.typus == TRAJ_METEOBLUE:
            self.gen_traj_meteoblue(args, packets, **kwargs)
            self.gen_markers_meteoblue(args, packets, **kwargs)

        if self.typus == TRACK_GPX:
            self.gen_track_gpx(args, packets, **kwargs)

        if self.typus == TRACK_SENSORLOG:
            self.gen_track_sensorlog(args, packets, **kwargs)

    def simulate_burner(self, args):
        BURNER_INTERVAL_MU = 15
        BURNER_INTERVAL_SIGMA = 3

        BURNER_DURATION_MU = 3
        BURNER_DURATION_SIGMA = 1

        TEMP_INTERVAL = 20

        t = self.starttime()
        current = t
        end = self.stoptime()
        samples = []

        # default the number of burners
        samples.append({"interval": self.availability(), "value": 0})
        delta = 0
        while current < end:
            interval = round(random.gauss(BURNER_INTERVAL_MU, BURNER_INTERVAL_SIGMA))
            delta += interval
            current = t + timedelta(seconds=delta)
            burn = round(random.gauss(BURNER_DURATION_MU, BURNER_DURATION_SIGMA), 1)
            endofburn = t + timedelta(seconds=delta + burn)
            samples.append(
                {
                    "interval": format_datetime_like(current)
                    + "/"
                    + format_datetime_like(endofburn),
                    # "value" : "burner1-on",
                    # "value" :  [f"burner{i}" for i in list(range(random.randrange(1, args.burners+1)))],
                    "number": random.randrange(1, args.burners + 1),
                }
            )
        return samples

    def simulate_burner_transformations(self, args):
        BURNER_INTERVAL_MU = 15
        BURNER_INTERVAL_SIGMA = 3

        BURNER_DURATION_MU = 3
        BURNER_DURATION_SIGMA = 1

        TEMP_INTERVAL = 20

        cart0 = [0.1, 0.1, 0.1]
        cart1 = [1, 1, 1]

        t = self.starttime()
        current = t
        end = self.stoptime()
        samples = []

        samples.append({"interval": self.availability(), "cartesian": cart0})

        delta = 0
        while current < end:
            interval = round(random.gauss(BURNER_INTERVAL_MU, BURNER_INTERVAL_SIGMA))
            delta += interval
            current = t + timedelta(seconds=delta)
            burn = round(random.gauss(BURNER_DURATION_MU, BURNER_DURATION_SIGMA), 1)
            endofburn = t + timedelta(seconds=delta + burn)
            samples.append(
                {
                    "interval": format_datetime_like(current)
                    + "/"
                    + format_datetime_like(endofburn),
                    "cartesian": cart1,
                }
            )
        return samples

    def simulate_temperature(self, args):
        TEMP_MU = 70
        TEMP_SIGMA = 10
        TEMP_INTERVAL = 20

        t = self.starttime()
        current = t
        end = self.stoptime()
        samples = []
        delta = 0
        while current < end:
            delta += TEMP_INTERVAL
            current = t + timedelta(seconds=delta)
            temp = round(random.gauss(TEMP_MU, TEMP_SIGMA))
            samples.append(delta)
            samples.append(temp)
        return {
            "epoch": format_datetime_like(self.starttime()),
            "interpolationAlgorithm": "LINEAR",
            # "interpolationDegree": 3,
            "number": samples,
        }

    # def simulate_vehicle_heading(self, args):
    #     ROTATION_MU = 360
    #     ROTATION_SIGMA = 120

    #     t = self.starttime()
    #     current = t
    #     end = self.stoptime()
    #     samples = [0, 0]
    #     delta = 0
    #     while current < end:
    #         interval = round(random.gauss(ROTATION_MU, ROTATION_SIGMA))
    #         delta += interval
    #         current = t + timedelta(seconds=delta)
    #         rot = round(random.uniform(0, 180))
    #         samples.append(delta)
    #         samples.append(rot)

    #     return {
    #         "epoch": format_datetime_like(self.starttime()),
    #         "interpolationAlgorithm": "LINEAR",
    #         # "interpolationDegree": 3,
    #         "number": samples,
    #     }

    # def simulate_vehicle_pitch(self, args):
    #     PITCH_GROUND = 90
    #     PITCH_ERECT = 0
    #     PITCH_T0 = 0
    #     PITCH_T1 =  PITCH_T0 + 15
    #     PITCH_T2 = 40

    #     samples = [0, PITCH_GROUND,
    #                PITCH_T0, PITCH_GROUND,
    #                PITCH_T1, PITCH_ERECT,
    #                self.duration()-PITCH_T2, PITCH_ERECT,
    #                self.duration(), PITCH_GROUND]

    #     return {
    #         "epoch": format_datetime_like(self.starttime()),
    #         "interpolationAlgorithm": "LINEAR",
    #         # "interpolationDegree": 3,
    #         "number": samples,
    #     }

    # def simulate_vehicle_roll(self, args):
    #     # no roll movement
    #     samples = [0, 0,
    #                self.duration(), 0]
    #     return {
    #         "epoch": format_datetime_like(self.starttime()),
    #         "interpolationAlgorithm": "LINEAR",
    #         # "interpolationDegree": 3,
    #         "number": samples,
    #     }

    def simulate_sensors(self, args):

        return {
            # "animations": self.simulate_burner(args),
            "burners": self.simulate_burner(args),
            "temperature": self.simulate_temperature(args),
            "foobar": {"value": {"foo": 4, "bar": 10}},
            "an_array": {"value": [1, 2, 3, 4, 5]},
            # "vehicle_heading": self.simulate_vehicle_heading(args),
            # "vehicle_pitch": self.simulate_vehicle_pitch(args),
            # "vehicle_roll": self.simulate_vehicle_roll(args),
        }


def prolog(packets, name, mintime, maxtime, multiplier=60, description="prolog"):

    clock = None
    if maxtime > mintime:
        currentTime = mintime.strftime("%Y-%m-%dT%H:%M:%SZ")
        interval = (
            mintime.strftime("%Y-%m-%dT%H:%M:%SZ")
            + "/"
            + maxtime.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        range = "CLAMPED"
        step = "SYSTEM_CLOCK_MULTIPLIER"

        start = mintime
        clock = IntervalValue(
            start=mintime,
            end=maxtime,
            value=Clock(currentTime=start, multiplier=multiplier),
        )

    preamble = Preamble(name="document", description=description, clock=clock)
    packets.append(preamble)


def parse_ext(track):
    # collapse all track-level extensions into a dict
    result = dict()
    for ext in track.extensions:
        if len(ext):
            for extchild in list(ext):
                result[extchild.tag] = extchild.text
        else:
            result[ext.tag] = ext.text
    return result


def csv2gpx(fnp):
    # fr24 track download
    # Timestamp,UTC,Callsign,Position,Altitude,Speed,Direction
    # 1624098163,2021-06-19T10:22:43Z,OEKGO,"46.936798,15.387732",2000,100,0
    with open(fnp, newline="") as csvfile:
        dialect = csv.Sniffer().sniff(csvfile.read(1024))
        csvfile.seek(0)
        # print(dialect.has_header())
        line = 0
        reader = csv.reader(csvfile, dialect)
        row = next(reader)
        if row == [
            "Timestamp",
            "UTC",
            "Callsign",
            "Position",
            "Altitude",
            "Speed",
            "Direction",
        ]:
            logging.debug(f"{fnp}: flightradar24 csv file")
            gpx = gpxpy.gpx.GPX()

            # Create first track in our GPX:
            gpx_track = gpxpy.gpx.GPXTrack()
            gpx.tracks.append(gpx_track)

            # Create first segment in our GPX track:
            gpx_segment = gpxpy.gpx.GPXTrackSegment()
            gpx_track.segments.append(gpx_segment)
            for row in reader:
                dt = dateutil.parser.parse(row[1])
                line += 1
                # print(row['UTC'], row['Position'])
                lat, lon = row[3].split(",")
                pt = gpxpy.gpx.GPXTrackPoint(
                    float(lat), float(lon), elevation=float(row[4]), time=dt
                )
                gpx_segment.points.append(pt)

            gpx.refresh_bounds()
            extremes = gpx.get_elevation_extremes()
            len = gpx.length_2d() / 1000
            dur = gpx.get_duration() / 3600
            start, stop = gpx.get_time_bounds()

            gpx.creator = f"Flightradar24 csv file "
            gpx.name = fnp
            desc = "callsign: " + row[2]
            desc += f" length={len:.2f}km duration={dur:.1f}h min alt={extremes.minimum}m max alt={extremes.maximum}"
            desc += f" start: {start} stop {stop}"
            gpx.description = desc
            return gpx


def igc2gpx(args, header, fix_records, igc_fn):
    gpx = gpxpy.gpx.GPX()

    # Create first track in our GPX:
    gpx_track = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(gpx_track)

    # Create first segment in our GPX track:
    gpx_segment = gpxpy.gpx.GPXTrackSegment()
    gpx_track.segments.append(gpx_segment)
    day = header["utc_date"]
    for fr in fix_records:
        dt = pytz.timezone("UTC").localize(datetime.combine(day, fr["time"]))
        pt = gpxpy.gpx.GPXTrackPoint(
            fr["lat"], fr["lon"], elevation=fr["gps_alt"], time=dt
        )
        gpx_segment.points.append(pt)
    gpx.refresh_bounds()
    len = gpx.length_2d() / 1000
    dur = gpx.get_duration() / 3600
    extremes = gpx.get_elevation_extremes()

    gpx.creator = f"{header['logger_model']} by {header['logger_manufacturer']} "
    gpx.creator += f"firmware={header['firmware_revision']} "
    gpx.author_name = header["pilot"]
    gpx.name = igc_fn

    desc = ""
    if header["glider_registration"] != "NKN":
        desc += f"{header['glider_registration']} "
    if header["glider_model"] != "NKN":
        desc += f"{header['glider_model']} "

    desc += f"length={len:.2f}km duration={dur:.1f}h min alt={extremes.minimum}m max alt={extremes.maximum}"
    gpx.description = desc

    # ugly haque around "TypeError: Object of type int64 is not JSON serializable"
    return gpxpy.parse(gpx.to_xml(version="1.0").encode("utf8"))


def main():
    ap = argparse.ArgumentParser(
        usage="%(prog)s [-s] [-m] [-d] [file ...]",
        description="convert a GPX track and/or Windy trajectory to CZML",
    )
    ap.add_argument(
        "-S",
        "--simplify",
        action="store",
        dest="distance",
        default=0.0,
        type=float,
        help="set simplify distance for plain GPX tracks in m",
    )
    ap.add_argument(
        "-m",
        "--model-uri",
        action="store",
        dest="model_uri",
        default=DEFAULT_MODEL,
        help="set the model URI",
    )
    ap.add_argument(
        "-n",
        "--doc-name",
        action="store",
        dest="docname",
        default="track visualisation",
        help="the name field in the document packet, also used for the GPS track",
    )
    ap.add_argument(
        "-C",
        "--doc-comment",
        action="store",
        dest="doccomment",
        default="description",
        help="comment, used in the track description field",
    )
    ap.add_argument("-d", "--debug", action="store_true", help="show detailed logging")
    ap.add_argument(
        "--fake-sensors",
        action="store_true",
        help="insert simulated burner/temperature properties",
    )
    ap.add_argument(
        "--burners",
        action="store",
        dest="burners",
        default=2,
        type=int,
        help="number of burners to simulate",
    )
    ap.add_argument(
        "--fake-orient",
        action="store_true",
        help="insert simulated orientation properties",
    )
    ap.add_argument(
        "--velocity-orient",
        action="store_true",
        help="orient aircraft based on heading, derived from velocity vector",
    )
    ap.add_argument(
        "--gyro",
        action="store_true",
        help="use gyro heading pitch roll; default to magnetometer",
    )
    ap.add_argument("-T", "--traj", action="append", default=[])
    ap.add_argument("-t", "--track", action="append", default=[])
    ap.add_argument(
        "--skip-seconds",
        action="store",
        dest="skipsecs",
        default=0.0,
        type=float,
        help="start at <arg> seconds into the track",
    )
    ap.add_argument(
        "--trim-seconds",
        action="store",
        dest="trimsecs",
        default=0.0,
        type=float,
        help="trim <arg> seconds off end of the track",
    )
    ap.add_argument(
        "-M",
        "--multiplier",
        action="store",
        dest="multiplier",
        default=60.0,
        type=float,
        help="animation playback time multiplier",
    )
    ap.add_argument(
        "-I",
        "--degree",
        action="store",
        dest="degree",
        default=1,
        type=int,
        help="degree for interpolation",
    )

    ap.add_argument(
        "--algo",
        default="LINEAR",
        type=str.upper,
        choices=["LINEAR", "LAGRANGE", "HERMITE"],
        help="interpolation algo for tracks: LINEAR LAGRANGE HERMITE (default: %(default)s)",
    )

    ap.add_argument(
        "-D",
        "--delta-h",
        action="store",
        dest="delta_h",
        default=default_delta,
        type=float,
        help="model altitude correction",
    )
    args, extra = ap.parse_known_args()

    level = logging.WARNING
    if args.debug:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(funcName)s:%(lineno)s %(message)s")
    logging.debug(f"{args=}")

    docname = args.docname
    result = list()
    maxtime = datetime(MINYEAR, 1, 1, tzinfo=pytz.UTC)
    mintime = datetime(MAXYEAR, 12, 31, 23, 59, 59, 999999, tzinfo=pytz.UTC)

    for filename in args.track:
        fnp = pathlib.Path(filename)
        if fnp.suffix == ".gpx":
            with open(filename, "r") as gpx_file:
                gpx = gpxpy.parse(gpx_file)
                i = 0
                for t in gpx.tracks:
                    gp = PathSet(gpx=gpx, track=t, typus=TRACK_GPX, filename=filename)
                    gp.ext = parse_ext(t)
                    begin, end = t.get_time_bounds()
                    logging.debug(f"{begin=}  {end=}")

                    mintime = min(mintime, begin)
                    maxtime = max(maxtime, end)
                    # gp.name = t.name if t.name else f"{fnp.stem}_{i}"
                    logging.debug(f"{filename}: plain GPX: {filename}")
                    i += 1

            gp.filename = filename
            result.append(gp)

        if fnp.suffix == ".csv":
            # fr24 track download
            # Timestamp,UTC,Callsign,Position,Altitude,Speed,Direction
            # 1624098163,2021-06-19T10:22:43Z,OEKGO,"46.936798,15.387732",2000,100,0
            gpx = csv2gpx(fnp)
            t = gpx.tracks[0]
            gp = PathSet(gpx=gpx, track=t, typus=TRACK_GPX, filename=filename)
            gp.filename = fnp
            logging.debug(f"{filename}: FR24 CSV: {gp.filename}")
            result.append(gp)

        if fnp.suffix == ".igc":
            r = Reader()
            try:
                with open(filename) as fd:
                    flight = r.read(fd)
                    header = flight["header"][1]
                    fix_records = flight["fix_records"][1]
                    # if args.debug:
                    #     pprint.pprint(header, indent=4)
                    #     pprint.pprint(fix_records, indent=4)

                    gpx = igc2gpx(args, header, fix_records, fnp.stem)
                    t = gpx.tracks[0]
                    gp = PathSet(gpx=gpx, track=t, typus=TRACK_GPX, filename=filename)
                    begin, end = t.get_time_bounds()
                    mintime = min(mintime, begin)
                    maxtime = max(maxtime, end)
                    gp.name = t.name if t.name else f"{fnp.stem}"

            except Exception:
                logging.exception(f"reading file {filename}")
                continue

            logging.debug(f"{filename}: IGC: {gp.name}")

            gp.filename = filename
            # result[gp.name] = gp
            result.append(gp)

        if fnp.suffix == ".json":
            with open(filename, "r") as json_file:
                jsarr = json.load(json_file)
                if len(jsarr) == 0:
                    logging.error(f"{fnp}: empty JSON track")
                    continue
                # test for sensorlog file
                if jsarr[0].keys() <= set(sensorlog_keys):
                    logging.debug(f"{fnp}: sensorlog format detected")
                else:
                    logging.error(f"{fnp}: cant decode json")
                    continue

                # logging.debug(f"{fnp}: {jsarr[0].keys()}")
                gp = PathSet(sensorlog=jsarr, typus=TRACK_SENSORLOG, filename=fnp)
                gp.name = "sensorlog " + str(fnp)

                mintime = min(mintime, gp.starttime())
                maxtime = max(maxtime, gp.stoptime(trim=FUDGE))

            result.append(gp)

    for filename in args.traj:
        fnp = pathlib.Path(filename)
        if fnp.suffix == ".gpx":
            with open(filename, "r") as gpx_file:
                gpx = gpxpy.parse(gpx_file)
                for t in gpx.tracks:
                    # print(gpx)
                    ext = parse_ext(t)
                    if {"model", "level", "distance"} <= set(ext):
                        gp = PathSet(
                            gpx=gpx, track=t, typus=TRAJ_WINDY_GPX, filename=filename
                        )
                        gp.ext = ext
                        gp.name = "windy " + gp.ext["model"] + " " + t.name

                        logging.debug(f"{filename=}, windy traj: {gp.name=} {gp.ext=}")

                    elif (
                        gpx.author_name == "meteoblue AG"
                    ) and gpx.author_email == "info@meteoblue.com":
                        gp = PathSet(
                            gpx=gpx, track=t, typus=TRAJ_METEOBLUE, filename=filename
                        )
                        gp.name = "meteoblue " + t.name
                        logging.debug(f"{filename=}, meteoblue traj: {gp.name}")

                    elif gpx.creator == "GPSBabel - https://www.gpsbabel.org":
                        gp = PathSet(
                            gpx=gpx, track=t, typus=TRAJ_METEOBLUE, filename=filename
                        )
                        gp.name = "gpsbabel"
                        logging.debug(f"{filename=}, gpsbabel traj: {gp.name}")
                    else:
                        logging.error(f"unrecognized trajectory format: {filename}")
                        continue

                    gp.filename = filename
                    result.append(gp)

        if fnp.suffix == ".kml":
            with open(filename, "r") as kml_file:
                gpx = gpxpy.gpx.GPX()
                kml = parser.parse(kml_file).getroot()
                logging.debug(kml.Document.Folder.name.text)
                gp = PathSet(kml=kml, typus=TRAJ_WINDY_KML, filename=filename)
                gp.name = "windy " + filename
                result.append(gp)

    mintime += timedelta(seconds=args.skipsecs)
    maxtime -= timedelta(seconds=args.trimsecs)

    logging.debug(
        (
            f'from {mintime.strftime("%Y-%m-%dT%H:%M:%SZ")} to'
            f'{maxtime.strftime("%Y-%m-%dT%H:%M:%SZ")}'
        )
    )

    packets = []

    prolog(
        packets,
        docname,
        mintime,
        maxtime,
        multiplier=args.multiplier,
        description=" ".join(sys.argv),
    )

    for path in result:
        logging.debug(f"generate {path=}")
        path.generate(args, packets)

    document = Document(packets)
    print(document.dumps(indent=4))  # , cls=NumpyEncoder)) #, default=np_encoder))


if __name__ == "__main__":
    main()
