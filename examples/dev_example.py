from gleaned.harvester import DataHarvester
from gleaned.datasources.wmi_source import WMIDataSource


# Callback function to handle real-time data
def live_data_callback(data):
    if data.empty:
        print("Callback invoked, but no data received.")
    else:
        print("New live data collected:")
        print(data)



# Initialize the harvester
harvester = DataHarvester()

# Register the WMI data source
wmi_source = WMIDataSource(refresh_interval=1)  # Poll every 5 seconds
harvester.register_source(wmi_source)

# Option 1: Harvest live data for 1 hour and save to a file
print("Harvesting live data for 3 seconds and saving to CSV...")
harvester.harvest_live_by_interval("BatteryStatus via WMI", duration=3, serialize_path="battery_data.csv")
print("Live data harvesting complete.")

# Option 2: Harvest live data continuously with a callback
print("Starting continuous live data harvesting...")
harvester.harvest_live_continuously("BatteryStatus via WMI", report_callback=live_data_callback)

# NOTE: The continuous harvesting will run indefinitely unless stopped manually.
# To stop it, you can press Ctrl+C or implement additional logic to terminate after a specific condition.
