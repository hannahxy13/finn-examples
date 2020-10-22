# Copyright (c) 2020 Xilinx, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of Xilinx nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import numpy as np
import os
import time
from pynq import Overlay, allocate
from pynq.ps import Clocks

from finn.util.data_packing import (
    finnpy_to_packed_bytearray,
    packed_bytearray_to_finnpy,
)

# Driver base class for FINN-generated dataflow accelerators.
# The particulars of the generated accelerator are specified via the
# io_shape_dict (generated by the MakePYNQDriver transformation).


class FINNExampleOverlay(Overlay):
    def __init__(
        self,
        bitfile_name,
        platform,
        io_shape_dict,
        batch_size=1,
        fclk_mhz=100.0,
        device=None,
        download=True,
        runtime_weight_dir="runtime_weights/",
    ):
        """Initialize the FINN accelerator.

        Parameters
        ----------
        bitfile_name: str
            Path to accelerator .bit/.xclbin file
        platform: str
            FINN platform type, either "alveo" or "zynq-iodma"
        io_shape_dict: dict
            Dictionary with particulars of the generated accelerator
        batch_size: int
            Maximum batch size in driver (hardware batchsize is always 1)
        fclk_mhz: float
            Override the clock frequency, only possible for Zynq.
        device: pynq.Device
            Which PYNQ device to use, None for default.
        download: bool
            Whether to flash the bitstream.
        runtime_weight_dir: str
            Path to runtime weights folder.
        """
        super().__init__(bitfile_name, download=download, device=device)
        self.runtime_weight_dir = runtime_weight_dir
        self._io_shape_dict = io_shape_dict
        self.ibuf_packed_device = None
        self.obuf_packed_device = None
        self.platform = platform
        self.batch_size = batch_size
        self.fclk_mhz = fclk_mhz
        if self.platform == "alveo":
            self.idma = self.idma0
            self.odma = self.odma0
            self.odma_handle = None
        elif self.platform == "zynq-iodma":
            self.idma = self.idma0
            self.odma = self.odma0
            # set the clock frequency as specified by user during transformations
            if self.fclk_mhz > 0:
                Clocks.fclk0_mhz = self.fclk_mhz
        else:
            raise ValueError("Supported platforms are zynq-iodma alveo")
        # load any runtime weights
        self.load_runtime_weights()

    def load_runtime_weights(self, flush_accel=True, verify=True):
        """Load any existing runtime weights from the specified dir into the
        appropriate layer of the accelerator. Note that this must be enabled
        during the accelerator build process. The runtime weights directory
        is specified as the class member ``runtime_weight_dir``.

        Parameters
        ----------
        flush_accel: bool
            Run the accelerator with dummy input after weights are written to
            flush any stale weight data in the weight streamer FIFOs.
        verify: bool
            Whether the written weights will be re-read and verified.
        """
        w_filenames = []
        if not os.path.isdir(self.runtime_weight_dir):
            return
        for (dirpath, dirnames, filenames) in os.walk(self.runtime_weight_dir):
            w_filenames.extend(filenames)
        rt_weight_dict = {}
        for w_filename in w_filenames:
            if w_filename.endswith(".dat"):
                with open(self.runtime_weight_dir + "/" + w_filename, "r") as f:
                    dat = f.read()
            layer_w = np.fromiter(
                [int(x, 16) for x in dat.strip().split()], dtype=np.uint32
            )
            layer_ind = int(w_filename.split("_")[0])
            rt_weight_dict[layer_ind] = layer_w
        for layer_ind in rt_weight_dict.keys():
            cand_if_name = "StreamingDataflowPartition_1/s_axilite_%d" % layer_ind
            if cand_if_name in self.ip_dict.keys():
                layer_mmio = getattr(
                    self.StreamingDataflowPartition_1, "s_axilite_%d" % layer_ind
                ).mmio
                layer_w = rt_weight_dict[layer_ind]
                layer_mmio.write_mm(0, layer_w.tobytes())
                if verify:
                    new_w = np.copy(layer_mmio.array[: layer_w.shape[0]])
                    assert (layer_w == new_w).all()
        if flush_accel:
            # run accelerator to flush any stale weights from weight streamer FIFOs
            self.execute_on_buffers()

    @property
    def idt(self):
        return self._io_shape_dict["idt"]

    @property
    def odt(self):
        return self._io_shape_dict["odt"]

    @property
    def ishape_normal(self):
        ret = list(self._io_shape_dict["ishape_normal"])
        ret[0] = self.batch_size
        return tuple(ret)

    @property
    def oshape_normal(self):
        ret = list(self._io_shape_dict["oshape_normal"])
        ret[0] = self.batch_size
        return tuple(ret)

    @property
    def ishape_folded(self):
        ret = list(self._io_shape_dict["ishape_folded"])
        ret[0] = self.batch_size
        return tuple(ret)

    @property
    def oshape_folded(self):
        ret = list(self._io_shape_dict["oshape_folded"])
        ret[0] = self.batch_size
        return tuple(ret)

    @property
    def ishape_packed(self):
        ret = list(self._io_shape_dict["ishape_packed"])
        ret[0] = self.batch_size
        return tuple(ret)

    @property
    def oshape_packed(self):
        ret = list(self._io_shape_dict["oshape_packed"])
        ret[0] = self.batch_size
        return tuple(ret)

    @property
    def batch_size(self):
        return self._batch_size

    @batch_size.setter
    def batch_size(self, value):
        self._batch_size = value
        # free the old buffers by setting to None
        # (reference counting should care of it)
        if self.ibuf_packed_device is not None:
            self.ibuf_packed_device = None
        if self.obuf_packed_device is not None:
            self.obuf_packed_device = None
        if self.platform == "alveo":
            self.ibuf_packed_device = allocate(shape=self.ishape_packed, dtype=np.uint8)
            self.obuf_packed_device = allocate(shape=self.oshape_packed, dtype=np.uint8)
        else:
            self.ibuf_packed_device = allocate(
                shape=self.ishape_packed, dtype=np.uint8, cacheable=True
            )
            self.obuf_packed_device = allocate(
                shape=self.oshape_packed, dtype=np.uint8, cacheable=True
            )
        self.obuf_packed = np.empty_like(self.obuf_packed_device)

    def fold_input(self, ibuf_normal):
        """Reshapes input in desired shape.
        Gets input data (ibuf_normal), checks if data is in expected normal shape.
        Returns folded input."""
        # ensure that shape is as expected
        assert ibuf_normal.shape == self.ishape_normal
        # convert to folded form
        ibuf_folded = ibuf_normal.reshape(self.ishape_folded)
        return ibuf_folded

    def pack_input(self, ibuf_folded):
        """Packs folded input and reverses both SIMD dim and endianness.
        Gets input data in folded shape and returns packed input data."""
        ibuf_packed = finnpy_to_packed_bytearray(
            ibuf_folded,
            self.idt,
            reverse_endian=True,
            reverse_inner=True,
            fast_mode=True,
        )
        return ibuf_packed

    def unpack_output(self, obuf_packed):
        """Unpacks the packed output buffer from accelerator.
        Gets packed output and returns output data in folded shape."""
        obuf_folded = packed_bytearray_to_finnpy(
            obuf_packed,
            self.odt,
            self.oshape_folded,
            reverse_endian=True,
            reverse_inner=True,
            fast_mode=True,
        )
        return obuf_folded

    def unfold_output(self, obuf_folded):
        """Unfolds output data to normal shape.
        Gets folded output data and returns output data in normal shape."""
        obuf_normal = obuf_folded.reshape(self.oshape_normal)
        return obuf_normal

    def copy_input_data_to_device(self, data):
        """Copies given input data to PYNQ buffer."""
        np.copyto(self.ibuf_packed_device, data)
        self.ibuf_packed_device.flush()

    def copy_output_data_from_device(self, data):
        """Copies PYNQ output buffer from device."""
        self.obuf_packed_device.invalidate()
        np.copyto(data, self.obuf_packed_device)

    def execute_on_buffers(self, asynch=False, batch_size=None):
        """Executes accelerator by setting up the DMA(s) on pre-allocated buffers.
        Blocking behavior depends on the asynch parameter:
        * ``asynch=True`` will block until all transfers are complete.
        * ``asynch=False`` won't block, use ``wait_until_finished()`` to check
           completion

        The optional batch_size parameter can be used to execute on a smaller
        batch than the initialized ``self.batch_size``.
        """
        if batch_size is None:
            batch_size = self.batch_size
        assert batch_size <= self.batch_size, "Specified batch_size is too large."
        if self.platform == "zynq-iodma":
            assert self.odma.read(0x00) & 0x4 != 0, "Output DMA is not idle"
            # manually launch IODMAs since signatures are missing
            self.idma.write(0x10, self.ibuf_packed_device.device_address)
            self.idma.write(0x1C, batch_size)
            self.odma.write(0x10, self.obuf_packed_device.device_address)
            self.odma.write(0x1C, batch_size)
            self.idma.write(0x00, 1)
            self.odma.write(0x00, 1)
        elif self.platform == "alveo":
            assert self.odma_handle is None, "Output DMA is already running"
            self.idma.start(self.ibuf_packed_device, batch_size)
            self.odma_handle = self.odma.start(self.obuf_packed_device, batch_size)
        else:
            raise Exception("Unrecognized platform: %s" % self.platform)
        # blocking behavior depends on asynch parameter
        if asynch is False:
            self.wait_until_finished()

    def wait_until_finished(self):
        "Block until the output DMA has finished writing."
        if self.platform == "zynq-iodma":
            # check if output IODMA is finished via register reads
            status = self.odma.read(0x00)
            while status & 0x2 == 0:
                status = self.odma.read(0x00)
        elif self.platform == "alveo":
            assert self.odma_handle is not None, "No odma_handle to wait on"
            self.odma_handle.wait()
            self.odma_handle = None
        else:
            raise Exception("Unrecognized platform: %s" % self.platform)

    def execute(self, input_npy):
        """Given input numpy array, first perform necessary packing and copying
        to device buffers, execute on accelerator, then unpack output and return
        output numpy array from accelerator."""
        ibuf_folded = self.fold_input(input_npy)
        ibuf_packed = self.pack_input(ibuf_folded)
        self.copy_input_data_to_device(ibuf_packed)
        self.execute_on_buffers()
        self.copy_output_data_from_device(self.obuf_packed)
        obuf_folded = self.unpack_output(self.obuf_packed)
        obuf_normal = self.unfold_output(obuf_folded)
        return obuf_normal

    def throughput_test(self):
        """Run accelerator with empty inputs to measure throughput and other metrics.
        Returns dictionary with various metrics."""
        # dictionary for results of throughput test
        res = {}
        start = time.time()
        self.execute_on_buffers()
        end = time.time()
        runtime = end - start
        res["runtime[ms]"] = runtime * 1000
        res["throughput[images/s]"] = self.batch_size / runtime
        res["DRAM_in_bandwidth[Mb/s]"] = (
            np.prod(self.ishape_packed) * 0.000001 / runtime
        )
        res["DRAM_out_bandwidth[Mb/s]"] = (
            np.prod(self.oshape_packed) * 0.000001 / runtime
        )
        if self.platform != "alveo":
            res["fclk[mhz]"] = Clocks.fclk0_mhz
        else:
            res["fclk[mhz]"] = self.fclk_mhz
        res["batch_size"] = self.batch_size
        # also benchmark driver-related overheads
        input_npy = np.zeros(self.ishape_normal, dtype=self.idt.to_numpy_dt())
        start = time.time()
        ibuf_folded = self.fold_input(input_npy)
        end = time.time()
        runtime = end - start
        res["fold_input[ms]"] = runtime

        start = time.time()
        ibuf_packed = self.pack_input(ibuf_folded)
        end = time.time()
        runtime = end - start
        res["pack_input[ms]"] = runtime

        start = time.time()
        self.copy_input_data_to_device(ibuf_packed)
        end = time.time()
        runtime = end - start
        res["copy_input_data_to_device[ms]"] = runtime

        start = time.time()
        self.copy_output_data_from_device(self.obuf_packed)
        end = time.time()
        runtime = end - start
        res["copy_output_data_from_device[ms]"] = runtime

        start = time.time()
        obuf_folded = self.unpack_output(self.obuf_packed)
        end = time.time()
        runtime = end - start
        res["unpack_output[ms]"] = runtime

        start = time.time()
        self.unfold_output(obuf_folded)
        end = time.time()
        runtime = end - start
        res["unfold_output[ms]"] = runtime
        return res