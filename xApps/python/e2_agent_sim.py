#!/usr/bin/env python3
"""
O-RAN E2 Node Agent Simulator for srsRAN Project / O-RAN SC RIC
Emulates an E2 Node (gNodeB DU/CU) with E2SM-KPM v3/v4 support.
Handles E2AP Subscription Requests from Subscription Manager and delivers
real-time radio measurement indications (throughput, PRB, volume, CQI) to xApps.
"""

import sys
import os
import time
import math
import random
import threading
import ctypes
from lib.asn1.e2sm_kpm_packer import e2sm_kpm_packer
from ricxappframe.xapp_frame import rmr

# Ctypes structures matching E2AP_if.h in libe2ap_wrapper.so
class RICRequestID_t(ctypes.Structure):
    _fields_ = [
        ('ricRequestorID', ctypes.c_uint32),
        ('ricInstanceID', ctypes.c_uint32)
    ]

class RICActionAdmittedItem_t(ctypes.Structure):
    _fields_ = [
        ('ricActionID', ctypes.c_uint64)
    ]

class RICActionAdmittedList_t(ctypes.Structure):
    _fields_ = [
        ('contentLength', ctypes.c_uint8),
        ('ricActionAdmittedItem', RICActionAdmittedItem_t * 16)
    ]

class RICCause_t(ctypes.Structure):
    _fields_ = [
        ('content', ctypes.c_uint8),
        ('cause', ctypes.c_uint8)
    ]

class RICActionNotAdmittedItem_t(ctypes.Structure):
    _fields_ = [
        ('ricActionID', ctypes.c_uint64),
        ('cause', RICCause_t)
    ]

class RICActionNotAdmittedList_t(ctypes.Structure):
    _fields_ = [
        ('contentLength', ctypes.c_uint8),
        ('ricActionNotAdmittedItem', RICActionNotAdmittedItem_t * 16)
    ]

class RICSubscriptionResponse_t(ctypes.Structure):
    _fields_ = [
        ('ricRequestID', RICRequestID_t),
        ('ranFunctionID', ctypes.c_uint16),
        ('ricActionAdmittedList', RICActionAdmittedList_t),
        ('ricActionNotAdmittedListPresent', ctypes.c_bool),
        ('ricActionNotAdmittedList', RICActionNotAdmittedList_t)
    ]

class RICSubscriptionDeleteResponse_t(ctypes.Structure):
    _fields_ = [
        ('ricRequestID', RICRequestID_t),
        ('ranFunctionID', ctypes.c_uint16)
    ]

class E2AgentSimulator:
    def __init__(self, rmr_port=4565, node_id="gnb_001_001_0000019b"):
        self.rmr_port = rmr_port
        self.node_id = node_id
        self.running = True
        self.active_subscriptions = {}
        self.packer = e2sm_kpm_packer()

        # Load ASN1 C library wrappers
        ctypes.CDLL('/usr/local/lib/libe2ap.so', mode=ctypes.RTLD_GLOBAL)
        self.libe2ap_wrapper = ctypes.CDLL('/usr/local/lib/libe2ap_wrapper.so')

        self.pack_sub_resp = self.libe2ap_wrapper.packRICSubscriptionResponse
        self.pack_sub_resp.restype = ctypes.c_uint64
        self.pack_sub_resp.argtypes = [
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(RICSubscriptionResponse_t)
        ]

        self.pack_sub_del_resp = self.libe2ap_wrapper.packRICSubscriptionDeleteResponse
        self.pack_sub_del_resp.restype = ctypes.c_uint64
        self.pack_sub_del_resp.argtypes = [
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(RICSubscriptionDeleteResponse_t)
        ]

        # Initialize RMR client on port 4565
        initbind = str(self.rmr_port).encode('utf-8')
        print(f"[E2-Agent-Sim] Initializing RMR on port {self.rmr_port}...")
        self.rmr_client = rmr.rmr_init(initbind, rmr.RMR_MAX_RCV_BYTES, 0x00)
        while rmr.rmr_ready(self.rmr_client) == 0:
            time.sleep(0.5)
        print("[E2-Agent-Sim] RMR is ready!")

        # Start periodic measurement generation thread
        self.meas_thread = threading.Thread(target=self._measurement_loop, daemon=True)
        self.meas_thread.start()

    def _pack_sub_response(self, req_id, inst_id, ran_func_id, action_ids):
        resp = RICSubscriptionResponse_t()
        resp.ricRequestID.ricRequestorID = req_id
        resp.ricRequestID.ricInstanceID = inst_id
        resp.ranFunctionID = ran_func_id
        resp.ricActionAdmittedList.contentLength = len(action_ids)
        for i, aid in enumerate(action_ids):
            resp.ricActionAdmittedList.ricActionAdmittedItem[i].ricActionID = aid
        resp.ricActionNotAdmittedListPresent = False

        pdu_len = ctypes.c_size_t(2048)
        pdu_buf = ctypes.create_string_buffer(2048)
        err_buf = ctypes.create_string_buffer(512)

        ret = self.pack_sub_resp(ctypes.byref(pdu_len), pdu_buf, err_buf, ctypes.byref(resp))
        if ret != 0:
            print(f"[E2-Agent-Sim] Error packing subscription response: {err_buf.value.decode()}")
            return None
        return bytes(pdu_buf[:pdu_len.value])

    def _pack_sub_del_response(self, req_id, inst_id, ran_func_id):
        resp = RICSubscriptionDeleteResponse_t()
        resp.ricRequestID.ricRequestorID = req_id
        resp.ricRequestID.ricInstanceID = inst_id
        resp.ranFunctionID = ran_func_id

        pdu_len = ctypes.c_size_t(1024)
        pdu_buf = ctypes.create_string_buffer(1024)
        err_buf = ctypes.create_string_buffer(512)

        ret = self.pack_sub_del_resp(ctypes.byref(pdu_len), pdu_buf, err_buf, ctypes.byref(resp))
        if ret != 0:
            return None
        return bytes(pdu_buf[:pdu_len.value])

    def send_rmr_msg(self, mtype, payload, meid, sub_id=-1, xid=None):
        sbuf = rmr.rmr_alloc_msg(self.rmr_client, len(payload), mtype=mtype)
        rmr.set_payload_and_length(payload, sbuf)
        if xid:
            rmr.rmr_set_vctx(sbuf, xid)
        else:
            rmr.generate_and_set_transaction_id(sbuf)
        sbuf.contents.mtype = mtype
        sbuf.contents.sub_id = sub_id
        sbuf.contents.state = 0
        rmr.rmr_set_meid(sbuf, meid.encode('utf-8'))
        sbuf = rmr.rmr_send_msg(self.rmr_client, sbuf)
        return sbuf

    def _measurement_loop(self):
        step = 0
        total_dl_bytes = 1048576
        total_ul_bytes = 524288
        while self.running:
            time.sleep(1.0)
            if not self.active_subscriptions:
                continue

            step += 1
            # Realistic synthetic 5G radio traffic measurements (sinusoidal + noise)
            # Uplink & Downlink throughput in kbps
            dl_base = 12500 + int(4500 * math.sin(step * 0.2)) + random.randint(-400, 400)
            ul_base = 8200 + int(2800 * math.cos(step * 0.2)) + random.randint(-300, 300)
            cqi_val = max(4, min(15, 14 + int(random.uniform(-1, 1))))
            prb_dl = max(10, min(100, int(dl_base / 250)))
            prb_ul = max(5, min(100, int(ul_base / 200)))
            total_dl_bytes += int(dl_base * 125)
            total_ul_bytes += int(ul_base * 125)

            # NTP timestamp
            ntp_epoch_offset = 2208988800
            now_unix = time.time()
            ntp_ts = int((now_unix + ntp_epoch_offset)) << 32
            ts_bytes = ntp_ts.to_bytes(8, 'big')

            for inst_id, sub_info in list(self.active_subscriptions.items()):
                metrics_list = sub_info.get('metrics', ['DRB.UEThpDl', 'DRB.UEThpUl'])
                report_style = sub_info.get('style', 1)

                header_dict = {
                    'colletStartTime': ts_bytes,
                    'fileFormatversion': 'v4.0'
                }
                encoded_hdr = self.packer.asn1_compiler.encode('E2SM-KPM-IndicationHeader-Format1', header_dict)

                if report_style == 5:
                    # Style 5: UE-Level metrics
                    ue_records = []
                    ue_ids = sub_info.get('ue_ids', [0, 1])
                    for ue in ue_ids:
                        meas_records = []
                        meas_info_list = []
                        for m in metrics_list:
                            meas_info_list.append({'measType': ('measName', m), 'labelInfoList': [{'measLabel': {'noLabel': 'true'}}]})
                            if m == 'DRB.UEThpDl':
                                val = dl_base if ue == 0 else int(dl_base * 0.4)
                            elif m == 'DRB.UEThpUl':
                                val = ul_base if ue == 0 else int(ul_base * 0.3)
                            elif 'Prb' in m:
                                val = prb_dl if ue == 0 else int(prb_dl * 0.4)
                            elif 'CQI' in m:
                                val = cqi_val
                            elif 'VolumeDL' in m:
                                val = total_dl_bytes
                            elif 'VolumeUL' in m:
                                val = total_ul_bytes
                            else:
                                val = random.randint(10, 500)
                            meas_records.append(('integer', val))
                        ue_records.append({
                            'ueID': ('gNB-DU-UEID', {'gNB-CU-UE-F1AP-ID': ue}),
                            'measData': [{'measRecord': meas_records}],
                            'measInfoList': meas_info_list,
                            'granulPeriod': 1000
                        })
                    msg_dict = {
                        'indicationMessage-formats': ('indicationMessage-Format5', {
                            'matchingUEidList': ue_records
                        })
                    }
                else:
                    # Style 1: E2-Node-Level metrics
                    meas_records = []
                    meas_info_list = []
                    for m in metrics_list:
                        meas_info_list.append({'measType': ('measName', m), 'labelInfoList': [{'measLabel': {'noLabel': 'true'}}]})
                        if m == 'DRB.UEThpDl':
                            val = dl_base
                        elif m == 'DRB.UEThpUl':
                            val = ul_base
                        elif 'Prb' in m or 'prb' in m.lower():
                            val = prb_dl
                        elif 'CQI' in m:
                            val = cqi_val
                        elif 'VolumeDL' in m:
                            val = total_dl_bytes
                        elif 'VolumeUL' in m:
                            val = total_ul_bytes
                        elif 'Drop' in m or 'Loss' in m:
                            val = random.randint(0, 2)
                        elif 'Success' in m:
                            val = 100
                        else:
                            val = random.randint(5, 500)
                        meas_records.append(('integer', val))

                    msg_dict = {
                        'indicationMessage-formats': ('indicationMessage-Format1', {
                            'measData': [{'measRecord': meas_records}],
                            'measInfoList': meas_info_list,
                            'granulPeriod': 1000
                        })
                    }

                encoded_msg = self.packer.asn1_compiler.encode('E2SM-KPM-IndicationMessage', msg_dict)

                # Framed payload: 4-byte header length + header + message
                payload = len(encoded_hdr).to_bytes(4, 'big') + encoded_hdr + encoded_msg
                # Send RIC_INDICATION (12050)
                self.send_rmr_msg(12050, payload, self.node_id, sub_id=inst_id)
                print(f"[E2-Agent-Sim] Emitted RIC_INDICATION for sub_id={inst_id}, metrics={metrics_list}")

    def run(self):
        print(f"[E2-Agent-Sim] Listening for E2AP messages on RMR port {self.rmr_port}...")
        while self.running:
            try:
                sbuf = rmr.rmr_torcv_msg(self.rmr_client, None, 200)
                summary = rmr.message_summary(sbuf)
            except Exception:
                continue

            if summary[rmr.RMR_MS_MSG_STATE] == 0:
                mtype = summary['message type']
                payload = rmr.get_payload(sbuf)
                xid = summary.get('xid', b'')
                meid = summary.get('meid', b'').decode('utf-8', errors='ignore')
                if not meid:
                    meid = self.node_id

                if mtype == 12010:  # RIC_SUB_REQ
                    print(f"\n[E2-Agent-Sim] >>> Received RIC_SUB_REQ from SubMgr! Payload len={len(payload)}")
                    # SubMgr assigns instance ID (usually 1, 2, 3...)
                    # Extract requestor id & instance id or parse ASN1
                    req_id = 123
                    inst_id = 1
                    # In SubMgr, instance id is tracked sequentially
                    if self.active_subscriptions:
                        inst_id = max(self.active_subscriptions.keys()) + 1

                    # Inspect action definition from payload if available
                    metrics = ['DRB.UEThpDl', 'DRB.UEThpUl']
                    style = 1
                    try:
                        # Attempt to parse KPM Action Definition from payload
                        # Action definition bytes typically appear after E2AP header
                        if b'DRB.' in payload:
                            # Extract any DRB.* metric names present in the payload string
                            import re
                            found_metrics = re.findall(rb'(DRB\.[A-Za-z0-9]+|CQI|RSRP|RRU\.[A-Za-z0-9]+)', payload)
                            if found_metrics:
                                metrics = [m.decode() for m in found_metrics]
                        if b'actionDefinition-Format5' in payload or b'\x05' in payload:
                            if b'ueID' in payload or b'matchingUEidList' in payload:
                                style = 5
                    except Exception as e:
                        print(f"[E2-Agent-Sim] Note during metric extraction: {e}")

                    print(f"[E2-Agent-Sim] Identified Subscribed Metrics: {metrics}, Report Style: {style}")

                    # Store subscription
                    self.active_subscriptions[inst_id] = {
                        'metrics': metrics,
                        'style': style,
                        'req_id': req_id,
                        'ue_ids': [0, 1]
                    }

                    # Send RIC_SUB_RESP (12011) back to SubMgr
                    resp_pdu = self._pack_sub_response(req_id, inst_id, 2, [1])
                    if resp_pdu:
                        self.send_rmr_msg(12011, resp_pdu, meid, sub_id=-1, xid=xid)
                        print(f"[E2-Agent-Sim] <<< Sent RIC_SUB_RESP (12011) to SubMgr: instance_id={inst_id}")

                elif mtype == 12020:  # RIC_SUB_DEL_REQ
                    print(f"\n[E2-Agent-Sim] >>> Received RIC_SUB_DEL_REQ! Stopping subscription.")
                    inst_id = 1
                    if self.active_subscriptions:
                        inst_id = list(self.active_subscriptions.keys())[0]
                        del self.active_subscriptions[inst_id]

                    del_resp_pdu = self._pack_sub_del_response(123, inst_id, 2)
                    if del_resp_pdu:
                        self.send_rmr_msg(12021, del_resp_pdu, meid, sub_id=-1, xid=xid)
                        print(f"[E2-Agent-Sim] <<< Sent RIC_SUB_DEL_RESP (12021) to SubMgr")

            rmr.rmr_free_msg(sbuf)

    def stop(self):
        self.running = False
        rmr.rmr_close(self.rmr_client)

if __name__ == '__main__':
    sim = E2AgentSimulator(rmr_port=4565)
    try:
        sim.run()
    except KeyboardInterrupt:
        sim.stop()
