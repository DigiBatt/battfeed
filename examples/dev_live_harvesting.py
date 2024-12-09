from gleaned.harvester import DataHarvester
from gleaned.datasources.wmi_source import WMIDataSource
import threading

# Initialize harvester
harvester = DataHarvester()
wmi_source = WMIDataSource(refresh_interval=1)
harvester.register_source(wmi_source)

# Start live harvesting
print("Harvesting live data for 3 seconds and saving to file...")
harvester.harvest_live("BatteryStatus via WMI", duration=5, serialize_path="battery_data.json", print_data=True)
