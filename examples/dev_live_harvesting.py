from gleaned.harvester import DataHarvester
from gleaned.datasources.wmi_source import WMIDataSource
import threading

def live_data_callback(data):
    if data.empty:
        print("Callback invoked, but no data received.")
    else:
        print("New live data collected:")
        print(data)

# Initialize harvester
harvester = DataHarvester()
wmi_source = WMIDataSource(refresh_interval=1)
harvester.register_source(wmi_source)

# Start continuous harvesting
print("Starting continuous live data harvesting...")
harvester.harvest_live_and_print("BatteryStatus via WMI", duration=5)

