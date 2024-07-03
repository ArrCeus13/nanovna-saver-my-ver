import logging
import os
import skrf as rf
import numpy as np
import scipy.fft
import matplotlib
matplotlib.use('Qt5Agg')  # Set the backend before importing pyplot
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from PyQt6 import QtWidgets
from NanoVNASaver.Analysis.Base import Analysis

logger = logging.getLogger(__name__)

class SParameterAnalysis(Analysis):
    def __init__(self, app):
        super().__init__(app)

        layout = self.layout
        layout.addRow("Processed Data Plot:", QtWidgets.QLabel("This will display the processed data plots"))

        self.figure, self.axs = plt.subplots(2, 1, figsize=(10, 10))  # Change to 2 subplots

        canvas = FigureCanvas(self.figure)
        layout.addWidget(canvas)
        self.canvas = canvas

    def runAnalysis(self):
        data_folder_path = 'src/NanoVNASaver/data_files'

        processed_data = []

        for filename in os.listdir(data_folder_path):
            if filename.endswith('.s1p'):
                file_path = os.path.join(data_folder_path, filename)
                rfdata = rf.Network(file_path)
                b = np.pad(rfdata.s[:, 0, 0], (0, 2048), 'constant', constant_values=(0))
                c = scipy.fft.irfft(b)
                cflip = c[0: int(c.shape[0] / 2)] + np.flip(c[int(c.shape[0] / 2):])
                processed_data.append(cflip)

        processed_data_array = np.array(processed_data)

        maxR = 200
        t = np.arange(0, maxR, maxR / processed_data_array.shape[1])

        self.axs[0].clear()
        for i, cflip in enumerate(processed_data):
            self.axs[0].plot(t[:200], cflip[:200], label=f'File {i+1}')
        self.axs[0].set_title('Processed Data Plot')
        self.axs[0].set_xlabel('Measurement index (second)')
        self.axs[0].set_ylabel('Amplitude')
        self.axs[0].legend()
        self.axs[0].grid(True)

        img = np.zeros((processed_data_array.shape[1], 10))
        for i in range(1, 11):
            if i <= len(processed_data):
                cflip = processed_data[i - 1]
                img[:, i - 1] = cflip.T

        self.axs[1].clear()
        self.axs[1].imshow(img[:200, :], aspect='auto', cmap='viridis')
        self.axs[1].set_title('Processed S-Parameter Data Image (First 10 Files)')
        self.axs[1].set_xlabel('Sample Index')
        self.axs[1].set_ylabel('Measurement Index (milisecond)')
        self.axs[1].grid(True)

        self.figure.tight_layout()
        self.canvas.draw()
