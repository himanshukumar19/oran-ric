#!/usr/bin/env python3
"""
Modified Radio Measurement Monitoring xApp (E2SM-KPM v3/v4)
Extended with additional radio performance metrics:
- DRB.UEThpDl                  : Downlink User Throughput (kbps)
- DRB.UEThpUl                  : Uplink User Throughput (kbps)
- DRB.RlcSduTransmittedVolumeDL : RLC Transmitted SDU Volume DL (Bytes)
- DRB.RlcSduTransmittedVolumeUL : RLC Transmitted SDU Volume UL (Bytes)
- CQI                          : Channel Quality Indicator [1-15]
- RRU.PrbTotDl                 : Total Downlink PRB Utilization
"""

import argparse
import signal
import sys
from lib.xAppBase import xAppBase


class ModifiedMonXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port):
        super(ModifiedMonXapp, self).__init__(config, http_server_port, rmr_port)
        self.sample_count = 0
        self.dl_thp_history = []
        self.ul_thp_history = []

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg):
        self.sample_count += 1
        print("\n" + "="*68)
        print(" [MODIFIED xAPP] RIC INDICATION #{} RECEIVED".format(self.sample_count))
        print(" Source E2 Agent : {}".format(e2_agent_id))
        print(" Subscription ID : {}".format(subscription_id))
        print(" Service Model   : ORAN-E2SM-KPM (Report Style 1)")
        print("="*68)

        indication_hdr = self.e2sm_kpm.extract_hdr_info(indication_hdr)
        meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)

        print(" Indication Header Timestamp: {}".format(indication_hdr['colletStartTime']))
        granulPeriod = meas_data.get("granulPeriod", 1000)
        print(" Granularity Period         : {} ms".format(granulPeriod))
        print("-" * 68)
        print(" {:<32} | {:<20} | {:<10}".format("METRIC NAME", "MEASURED VALUE", "UNIT"))
        print("-" * 68)

        for metric_name, values in meas_data["measData"].items():
            val = values[0] if isinstance(values, list) and len(values) > 0 else values
            unit = ""
            if "Thp" in metric_name:
                unit = "kbps"
                if "Dl" in metric_name:
                    self.dl_thp_history.append(val)
                else:
                    self.ul_thp_history.append(val)
            elif "Volume" in metric_name:
                unit = "Bytes"
            elif "CQI" in metric_name:
                unit = "Index [1-15]"
            elif "Prb" in metric_name:
                unit = "PRBs"
            elif "Rate" in metric_name or "Success" in metric_name:
                unit = "%"
            else:
                unit = "Count"

            # Format numbers with commas
            if isinstance(val, (int, float)):
                val_str = "{:,}".format(val)
            else:
                val_str = str(val)

            print(" {:<32} | {:<20} | {:<10}".format(metric_name, val_str, unit))

        print("-" * 68)
        if self.dl_thp_history and self.ul_thp_history:
            avg_dl = sum(self.dl_thp_history) / len(self.dl_thp_history)
            avg_ul = sum(self.ul_thp_history) / len(self.ul_thp_history)
            print(" [Statistical Summary] Samples: {} | Avg DL: {:.2f} kbps ({:.2f} Mbps) | Avg UL: {:.2f} kbps ({:.2f} Mbps)".format(
                self.sample_count, avg_dl, avg_dl / 1000.0, avg_ul, avg_ul / 1000.0
            ))
        print("="*68)

    @xAppBase.start_function
    def start(self, e2_node_id, metric_names):
        print("[ModifiedMonXapp] Subscribing to E2 node ID: {}, RAN func: e2sm_kpm".format(e2_node_id))
        print("[ModifiedMonXapp] Monitored Metrics: {}".format(metric_names))
        report_period = 1000
        granul_period = 1000
        self.e2sm_kpm.subscribe_report_service_style_1(
            e2_node_id, report_period, metric_names, granul_period, self.my_subscription_callback
        )


def get_available_port(preferred_port=8095):
    import socket
    for p in range(preferred_port, preferred_port + 50):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('0.0.0.0', p))
                return p
        except OSError:
            continue
    return preferred_port


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Modified Monitoring xApp with Extended Metrics')
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8095, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4561, help="RMR port")
    parser.add_argument("--e2_node_id", type=str, default='gnbd_001_001_00019b_0', help="E2 Node ID")
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID")
    parser.add_argument("--metrics", type=str,
                        default='DRB.UEThpDl,DRB.UEThpUl,DRB.RlcSduTransmittedVolumeDL,DRB.RlcSduTransmittedVolumeUL,CQI,RRU.PrbTotDl',
                        help="Metrics name as comma-separated string")

    args = parser.parse_args()
    config = args.config
    e2_node_id = args.e2_node_id
    ran_func_id = args.ran_func_id
    metrics = args.metrics.split(",")

    # Automatically resolve port conflicts
    server_port = get_available_port(args.http_server_port)

    # Create Modified xApp
    myXapp = ModifiedMonXapp(config, server_port, args.rmr_port)
    myXapp.e2sm_kpm.set_ran_func_id(ran_func_id)

    # Connect exit signals
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    # Start xApp
    myXapp.start(e2_node_id, metrics)
