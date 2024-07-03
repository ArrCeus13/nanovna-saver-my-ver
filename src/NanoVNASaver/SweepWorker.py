#  NanoVNASaver
#
#  A python program to view and export Touchstone data from a NanoVNA
#  Copyright (C) 2019, 2020  Rune B. Broberg
#  Copyright (C) 2020,2021 NanoVNA-Saver Authors
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
import logging
from time import sleep
import threading

import numpy as np
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import pyqtSlot, pyqtSignal

from NanoVNASaver.Calibration import correct_delay
from NanoVNASaver.RFTools import Datapoint
from NanoVNASaver.Settings.Sweep import Sweep, SweepMode

from NanoVNASaver.Touchstone import Touchstone
import datetime
import os
import time
import sys
from gpiozero import Button, PWMOutputDevice
from signal import pause

# Pin definition
DO_PIN = 2  # Digital output pin

# Global variable
high_count = 0

# Set up GPIO mode
try:
    sensor = Button(DO_PIN)
except Exception as e:
    print(f"Error setting up GPIO: {e}")
    sys.exit(1)
    
in1_motor1 = 5
in2_motor1 = 6
en_motor1 = 26
in1_motor2 = 27
in2_motor2 = 22
en_motor2 = 17

motor1_in1 = PWMOutputDevice(in1_motor1)
motor1_in2 = PWMOutputDevice(in2_motor1)
motor1_en = PWMOutputDevice(en_motor1)
motor2_in1 = PWMOutputDevice(in1_motor2)
motor2_in2 = PWMOutputDevice(in2_motor2)
motor2_en = PWMOutputDevice(en_motor2)

motor1_en.value = 1  # Enable motor 1
motor2_en.value = 1  # Enable motor 2

logger = logging.getLogger(__name__)

def truncate(values: list[list[tuple]], count: int) -> list[list[tuple]]:
    """truncate drops extrema from data list if averaging is active"""
    keep = len(values) - count
    logger.debug("Truncating from %d values to %d", len(values), keep)
    if count < 1 or keep < 1:
        logger.info("Not doing illegal truncate")
        return values
    truncated = []
    for valueset in np.swapaxes(values, 0, 1).tolist():
        avg = complex(*np.average(valueset, 0))
        truncated.append(
            sorted(valueset, key=lambda v, a=avg: abs(a - complex(*v)))[:keep]
        )
    return np.swapaxes(truncated, 0, 1).tolist()


class WorkerSignals(QtCore.QObject):
    updated = pyqtSignal()
    finished = pyqtSignal()
    sweepError = pyqtSignal()


class SweepWorker(QtCore.QRunnable):
    def __init__(self, app: QtWidgets.QWidget):
        super().__init__()
        logger.info("Initializing SweepWorker")
        self.signals = WorkerSignals()
        self.app = app
        self.sweep = Sweep()
        self.setAutoDelete(False)
        self.percentage = 0
        self.data11: list[Datapoint] = []
        self.data21: list[Datapoint] = []
        self.rawData11: list[Datapoint] = []
        self.rawData21: list[Datapoint] = []
        self.init_data()
        self.stopped = False
        self.running = False
        self.error_message = ""
        self.offsetDelay = 0
        self.IRcount = 0  # Counter for sensor HIGH state
        self.previous_state = False
        self.sensor_thread = threading.Thread(target=self.sensor_monitor)
        self.sensor_thread.daemon = True
        self.sensor_thread.start()
        # Sensor event handling
        # sensor.when_pressed = self.increment_high_count

    @pyqtSlot()
    def run(self) -> None:
        try:
            self._run()
        except BaseException as exc:  # pylint: disable=broad-except
            logger.exception("%s", exc)
            self.gui_error(f"ERROR during sweep\n\nStopped\n\n{exc}")
            if logger.isEnabledFor(logging.DEBUG):
                raise exc

    def _run(self) -> None:
        logger.info("Initializing SweepWorker")
        if not self.app.vna.connected():
            logger.debug(
                "Attempted to run without being connected to the NanoVNA"
            )
            self.running = False
            return

        self.running = True
        self.percentage = 0

        sweep = self.app.sweep.copy()

        if sweep != self.sweep:  # parameters changed
            self.sweep = sweep
            self.init_data()
         
        if sweep.properties.mode == SweepMode.CONTINOUS:
            self.run_motor(1, 'backward')
            self.run_motor(2, 'backward')

        self._run_loop()

        if sweep.segments > 1:
            start = sweep.start
            end = sweep.end
            logger.debug(
                "Resetting NanoVNA sweep to full range: %d to %d", start, end
            )
            self.app.vna.resetSweep(start, end)

        self.percentage = 100
        logger.debug('Sending "finished" signal')
        self.signals.finished.emit()
        self.running = False
        
        # Stop the motors
        self.stop_motor(1)
        self.stop_motor(2)
        # self.IRcount = 0  # Reset Count to 0

    def increment_high_count(self):
        self.IRcount += 1
        print(f"High count: {self.IRcount}")
        self.trigger_save()
      
    def run_motor(self, motor, direction):
        if motor == 1:
            motor1_in1.value = 1 if direction == 'forward' else 0
            motor1_in2.value = 0 if direction == 'forward' else 1
        elif motor == 2:
            motor2_in1.value = 1 if direction == 'forward' else 0
            motor2_in2.value = 0 if direction == 'forward' else 1
			
    def stop_motor(self, motor):
        if motor == 1:
            motor1_in1.off()
            motor1_in2.off()
        elif motor == 2:
            motor2_in1.off()
            motor2_in2.off()
            
    def sensor_monitor(self):
        while True:
            if self.stopped:
                break
            current_state = sensor.is_pressed
            if current_state and not self.previous_state:
                self.IRcount += 1
                print(f"High count: {self.IRcount}")
                self.trigger_save()
            self.previous_state = current_state
            sleep(0.001)
	  
    def _run_loop(self, nr_params: int = 1) -> None:
        sweep = self.sweep
        averages = (
            sweep.properties.averages[0]
            if sweep.properties.mode == SweepMode.AVERAGE
            else 1
        )
        logger.info("%d averages", averages)

        last_save_pulses = 0 # Initialize the last save pulses
        last_save_time = 0

        while True:
            for i in range(sweep.segments):
                logger.debug("Sweep segment no %d", i)
                if self.stopped:
                    logger.debug("Stopping sweeping as signalled")
                    break
                start, stop = sweep.get_index_range(i)

                freq, values11, values21 = self.readAveragedSegment(
                    start, stop, averages
                )
                self.percentage = (i + 1) * 100 / sweep.segments
                self.updateData(freq, values11, values21, i)

            if sweep.properties.mode != SweepMode.CONTINOUS or self.stopped:
                break

    def trigger_save(self, nr_params: int = 1) -> None:
        sweep = self.sweep
        if self.stopped:
            return
        pulse_count_threshold = sweep.properties.pulse_count
        if sweep.properties.mode == SweepMode.CONTINOUS and self.IRcount % pulse_count_threshold == 0:
			#Stop motor
            self.stop_motor(1)
            self.stop_motor(2)
            
            sleep(2)
            
			# Save S11 data (1-port)
            ts_str1 = self.touchstoneString(1)
            self.saveFile(ts_str1, "s1p")

            # Save S21 data (2-port)
            ts_str2 = self.touchstoneString(4)
            self.saveFile(ts_str2, "s2p")
            
            sleep(2)
            if not self.stopped:           
				#Start motor
                self.run_motor(1, 'backward')
                self.run_motor(2, 'backward')

    def touchstoneString(self, nr_params: int) -> str:
        ts = Touchstone()
        ts.sdata[0] = self.app.data.s11
        if nr_params > 1:
            ts.sdata[1] = self.app.data.s21
            for dp in self.app.data.s11:
                ts.sdata[2].append(Datapoint(dp.freq, 0, 0))
                ts.sdata[3].append(Datapoint(dp.freq, 0, 0))

        ts_str = "# HZ S RI R 50\n"
        for i, dp_s11 in enumerate(ts.s11):
            ts_str += f"{dp_s11.freq} {dp_s11.re} {dp_s11.im}"
            for j in range(1, nr_params):
                dp = ts.sdata[j][i]
                if dp.freq != dp_s11.freq:
                    raise LookupError("Frequencies of sdata not correlated")
                ts_str += f" {dp.re} {dp.im}"
            ts_str += "\n"
        return ts_str

    def saveFile(self, data_string, filename_suffix, directory="src/NanoVNASaver/data_files"):
        # Ensure the directory exists
        os.makedirs(directory, exist_ok=True)

        # Create a timestamped filename
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = os.path.join(directory, f"{timestamp}.{filename_suffix}")
        print("Debug - Saving data to:", filename)  # Debugging output

        # Write the data to the file
        try:
            with open(filename, 'w') as file:
                file.write(data_string)
            print(f"Data successfully saved to {filename}")
        except Exception as e:
            print(f"Failed to save data: {e}")

    def init_data(self):
        self.data11 = []
        self.data21 = []
        self.rawData11 = []
        self.rawData21 = []
        for freq in self.sweep.get_frequencies():
            self.data11.append(Datapoint(freq, 0.0, 0.0))
            self.data21.append(Datapoint(freq, 0.0, 0.0))
            self.rawData11.append(Datapoint(freq, 0.0, 0.0))
            self.rawData21.append(Datapoint(freq, 0.0, 0.0))
        logger.debug("Init data length: %s", len(self.data11))

    def updateData(self, frequencies, values11, values21, index):
        # Update the data from (i*101) to (i+1)*101
        logger.debug(
            "Calculating data and inserting in existing data at index %d", index
        )
        offset = self.sweep.points * index

        raw_data11 = [
            Datapoint(freq, values11[i][0], values11[i][1])
            for i, freq in enumerate(frequencies)
        ]
        raw_data21 = [
            Datapoint(freq, values21[i][0], values21[i][1])
            for i, freq in enumerate(frequencies)
        ]

        data11, data21 = self.applyCalibration(raw_data11, raw_data21)
        logger.debug("update Freqs: %s, Offset: %s", len(frequencies), offset)
        for i in range(len(frequencies)):
            self.data11[offset + i] = data11[i]
            self.data21[offset + i] = data21[i]
            self.rawData11[offset + i] = raw_data11[i]
            self.rawData21[offset + i] = raw_data21[i]

        logger.debug(
            "Saving data to application (%d and %d points)",
            len(self.data11),
            len(self.data21),
        )
        self.app.saveData(self.data11, self.data21)
        logger.debug('Sending "updated" signal')
        self.signals.updated.emit()

    def applyCalibration(
        self, raw_data11: list[Datapoint], raw_data21: list[Datapoint]
    ) -> tuple[list[Datapoint], list[Datapoint]]:
        data11: list[Datapoint] = []
        data21: list[Datapoint] = []

        if not self.app.calibration.isCalculated:
            data11 = raw_data11.copy()
            data21 = raw_data21.copy()
        elif self.app.calibration.isValid1Port():
            data11.extend(
                self.app.calibration.correct11(dp) for dp in raw_data11
            )
        else:
            data11 = raw_data11.copy()

        if self.app.calibration.isValid2Port():
            for counter, dp in enumerate(raw_data21):
                dp11 = raw_data11[counter]
                data21.append(self.app.calibration.correct21(dp, dp11))
        else:
            data21 = raw_data21

        if self.offsetDelay != 0:
            data11 = [
                correct_delay(dp, self.offsetDelay, reflect=True)
                for dp in data11
            ]
            data21 = [correct_delay(dp, self.offsetDelay) for dp in data21]

        return data11, data21

    def readAveragedSegment(self, start, stop, averages=1):
        values11 = []
        values21 = []
        freq = []
        logger.info(
            "Reading from %d to %d. Averaging %d values", start, stop, averages
        )
        for i in range(averages):
            if self.stopped:
                logger.debug("Stopping averaging as signalled.")
                if averages == 1:
                    break
                logger.warning("Stop during average. Discarding sweep result.")
                return [], [], []
            logger.debug("Reading average no %d / %d", i + 1, averages)
            retry = 0
            tmp11 = []
            tmp21 = []
            while not tmp11 and retry < 5:
                sleep(0.5 * retry)
                retry += 1
                freq, tmp11, tmp21 = self.readSegment(start, stop)
                if retry > 1:
                    logger.error(
                        "retry %s readSegment(%s,%s)", retry, start, stop
                    )
                    sleep(0.5)
            values11.append(tmp11)
            values21.append(tmp21)
            self.percentage += 100 / (self.sweep.segments * averages)
            self.signals.updated.emit()

        if not values11:
            raise IOError("Invalid data during swwep")

        truncates = self.sweep.properties.averages[1]
        if truncates > 0 and averages > 1:
            logger.debug("Truncating %d values by %d", len(values11), truncates)
            values11 = truncate(values11, truncates)
            values21 = truncate(values21, truncates)

        logger.debug("Averaging %d values", len(values11))
        values11 = np.average(values11, 0).tolist()
        values21 = np.average(values21, 0).tolist()

        return freq, values11, values21

    def readSegment(self, start, stop):
        logger.debug("Setting sweep range to %d to %d", start, stop)
        self.app.vna.setSweep(start, stop)

        frequencies = self.app.vna.readFrequencies()
        logger.debug("Read %s frequencies", len(frequencies))
        values11 = self.readData("data 0")
        values21 = self.readData("data 1")
        if not len(frequencies) == len(values11) == len(values21):
            logger.info("No valid data during this run")
            return [], [], []
        return frequencies, values11, values21

    def readData(self, data):
        logger.debug("Reading %s", data)
        done = False
        returndata = []
        count = 0
        while not done:
            done = True
            returndata = []
            tmpdata = self.app.vna.readValues(data)
            logger.debug("Read %d values", len(tmpdata))
            for d in tmpdata:
                a, b = d.split(" ")
                try:
                    if self.app.vna.validateInput and (
                        abs(float(a)) > 9.5 or abs(float(b)) > 9.5
                    ):
                        logger.warning(
                            "Got a non plausible data value: (%s)", d
                        )
                        done = False
                        break
                    returndata.append((float(a), float(b)))
                except ValueError as exc:
                    logger.exception(
                        "An exception occurred reading %s: %s", data, exc
                    )
                    done = False
            if not done:
                logger.debug("Re-reading %s", data)
                sleep(0.2)
                count += 1
                if count == 5:
                    logger.error(
                        "Tried and failed to read %s %d times.", data, count
                    )
                    logger.debug("trying to reconnect")
                    self.app.vna.reconnect()
                if count >= 10:
                    logger.critical(
                        "Tried and failed to read %s %d times. Giving up.",
                        data,
                        count,
                    )
                    raise IOError(
                        f"Failed reading {data} {count} times.\n"
                        f"Data outside expected valid ranges,"
                        f" or in an unexpected format.\n\n"
                        f"You can disable data validation on the"
                        f"device settings screen."
                    )
        return returndata

    def gui_error(self, message: str):
        self.error_message = message
        self.stopped = True
        self.running = False
        self.signals.sweepError.emit()
