import wmi
import pandas as pd
from datetime import datetime
from .base import DataSource

class WMIDataSource(DataSource):
    def __init__(self, namespace="root\\WMI", refresh_interval=5):
        super().__init__(refresh_interval)
        self.wmi = wmi.WMI(namespace=namespace)
        self.start_time = datetime.now()

    def collect_data(self) -> pd.DataFrame:
        """Collect battery data using WMI."""
        try:
            new_data = []
            for b in self.wmi.query("SELECT * FROM BatteryStatus"):
                new_data.append({
                    "Timestamp": datetime.now(),
                    "TestTime / s": (datetime.now() - self.start_time).total_seconds(),
                    "Voltage / V": getattr(b, "Voltage", None) / 1000 if getattr(b, "Voltage", None) else None,
                    "Remaining Capacity / Wh": getattr(b, "RemainingCapacity", None) / 1000 if getattr(b, "RemainingCapacity", None) else None,
                    "Discharge Rate / W": getattr(b, "DischargeRate", None) / 100000000 if getattr(b, "DischargeRate", None) else None,
                    "Charge Rate / W": getattr(b, "ChargeRate", None) / 100000000 if getattr(b, "ChargeRate", None) else None,
                })
            return pd.DataFrame(new_data)
        except Exception as e:
            print(f"WMI Query Failed: {e}")
            return pd.DataFrame()

    def metadata(self):
        """Return metadata about the WMI data source."""
        return {
            "source": "BatteryStatus via WMI",
            "namespace": "root\\WMI"
        }
