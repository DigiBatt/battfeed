import os
from gleaned.harvester import DataHarvester
from gleaned.datasources.file_source import FileSource
from gleaned.datasources.wmi_source import WMIDataSource

# Initialize the harvester
harvester = DataHarvester()

# Define the file path for the CSV file
extension = "json"
file_name = os.path.abspath(f"battery_data.{extension}")  # Convert to absolute path
print(f"Using file: {file_name}")

# Register the file source
file_source = FileSource(file_path=file_name, file_type=f"{extension}")
wmi_source = WMIDataSource(refresh_interval=1)
harvester.register_source([file_source, wmi_source])
#harvester.register_source(wmi_source)

# Use the exact source name from metadata
data = harvester.harvest_static(file_source)
if not data.empty:
    print(data.head())  # Display the first few rows
else:
    print("No data collected from the file.")


# Start live harvesting
print("Harvesting live data for 3 seconds and saving to file...")
harvester.harvest_live("BatteryStatus via WMI", duration=5, print_data=True)
