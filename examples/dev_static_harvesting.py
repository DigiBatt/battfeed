import os
from gleaned.harvester import DataHarvester
from gleaned.datasources.file_source import FileSource

# Initialize the harvester
harvester = DataHarvester()

# Define the file path for the CSV file
extension = "json"
file_name = os.path.abspath(f"battery_data.{extension}")  # Convert to absolute path
print(f"Using file: {file_name}")

# Register the file source
file_source = FileSource(file_path=file_name, file_type=f"{extension}")
harvester.register_source(file_source)

# Use the exact source name from metadata
data = harvester.harvest_static(file_source)
if not data.empty:
    print(data.head())  # Display the first few rows
else:
    print("No data collected from the file.")
