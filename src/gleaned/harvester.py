import pandas as pd
import threading
import time
from typing import Callable, Optional
from gleaned.datasources.base import DataSource

class DataHarvester:
    def __init__(self):
        self.sources = []
        self.collected_data = {}
        self._stop_flag = threading.Event()  # Shared flag to stop live harvesting threads

    def register_source(self, sources):
        """
        Register one or more data sources.
        :param sources: A single data source or a list of data sources.
        """
        if not isinstance(sources, list):
            sources = [sources]

        for source in sources:
            self.sources.append(source)
            self.collected_data[source.metadata()["source"]] = pd.DataFrame()
            print(f"Registered source: {source.metadata()['source']}")

    # Option 1: Harvest Static Data
    def harvest_static(self, source: DataSource) -> pd.DataFrame:
        """
        Harvest static data from a registered source.
        :param source: The data source object.
        :return: A DataFrame containing the harvested data.
        """
        print(f"Harvesting static data from source: {source.metadata()['source']}")
        data = source.collect_data()
        if data.empty:
            print(f"No data collected from {source.metadata()['source']}.")
        else:
            print(f"Collected {len(data)} rows from {source.metadata()['source']}.")
        return data


    # Option 2: Harvest Live Data by Interval
    def harvest_live(
        self, 
        source_name: str, 
        duration: int, 
        serialize_path: Optional[str] = None, 
        print_data: bool = False
    ):
        """
        Harvest live data for a specified duration.
        :param source_name: Name of the data source.
        :param duration: Duration in seconds for data collection.
        :param serialize_path: Path to save the data (optional).
        :param print_data: Whether to print collected data to the console.
        """
        source = self._get_source_by_name(source_name)
        print(f"Starting live data harvesting for {duration} seconds from source: {source_name}")
        start_time = time.time()

        while time.time() - start_time < duration and not self._stop_flag.is_set():
            try:
                data = source.collect_data()
                if not data.empty:
                    if print_data:
                        print(f"Collected {len(data)} rows from {source_name}:")
                        print(data)
                    
                    # Store collected data
                    self.collected_data[source.metadata()["source"]] = pd.concat(
                        [self.collected_data[source.metadata()["source"]], data], ignore_index=True
                    )
                else:
                    print(f"No data collected this cycle from {source_name}.")
            except Exception as e:
                print(f"Error while collecting data: {e}")
            time.sleep(source.refresh_interval)

        if serialize_path:
            self._serialize_data(source.metadata()["source"], serialize_path)
            print(f"Data saved to {serialize_path}")

        print("Live data harvesting complete.")


    # Option 3: Harvest Live Data Continuously
    def harvest_live_and_print(self, source_name: str, duration: int):
        """
        Harvest live data for a specified duration and print the values.
        :param source_name: Name of the data source.
        :param duration: Duration in seconds for data collection.
        """
        source = self._get_source_by_name(source_name)
        print(f"Starting live data harvesting for {duration} seconds from source: {source_name}")
        start_time = time.time()

        while time.time() - start_time < duration and not self._stop_flag.is_set():
            try:
                data = source.collect_data()
                if not data.empty:
                    print(f"Collected {len(data)} rows from {source_name}:")
                    print(data)
                else:
                    print(f"No data collected this cycle from {source_name}.")
            except Exception as e:
                print(f"Error while collecting data: {e}")
            time.sleep(source.refresh_interval)

        print("Live data harvesting complete.")


    def get_live_status(self, source_name: str) -> dict:
        """
        Get a one-time snapshot of the live status from a data source.
        :param source_name: Name of the data source.
        :return: A dictionary with the collected values or an empty dictionary if no data is collected.
        """
        source = self._get_source_by_name(source_name)
        print(f"Taking a snapshot of the live status from source: {source_name}")

        try:
            data = source.collect_data()
            if not data.empty:
                print(f"Snapshot collected with {len(data)} rows:")
                print(data)
                # Convert the first row of the dataframe to a dictionary
                return data.iloc[0].to_dict()
            else:
                print(f"No data collected from source: {source_name}")
        except Exception as e:
            print(f"Error while collecting snapshot: {e}")

        return {}



    def stop_harvesting(self):
        """Signal to stop all live harvesting."""
        print("Stopping live harvesting...")
        self._stop_flag.set()

    def _get_source_by_name(self, source_name: str):
        """Retrieve a registered source by its name."""
        for source in self.sources:
            if source.metadata()["source"] == source_name:
                return source
        raise ValueError(f"Source {source_name} not found.")

    def _serialize_data(self, source_name: str, file_path: str):
        """Save collected data to a file."""
        data = self.collected_data.get(source_name, pd.DataFrame())
        if data.empty:
            print(f"No data available for source: {source_name}")
            return

        try:
            if file_path.endswith(".csv"):
                data.to_csv(file_path, index=False)
            elif file_path.endswith(".parquet"):
                data.to_parquet(file_path)
            elif file_path.endswith(".json"):
                data.to_json(file_path, orient="records")
            else:
                raise ValueError(f"Unsupported file format for {file_path}")
            print(f"Data saved to {file_path}.")
        except Exception as e:
            print(f"Error while saving data to {file_path}: {e}")
