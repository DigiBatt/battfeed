"""
Gleaned: A Modular Data Harvesting Framework

Gleaned is a flexible and extensible framework for collecting and managing data 
from various live and static sources. It supports WMI-based battery data collection, 
CSV imports, and more, with built-in support for live updates and modular source integration.
"""

import pkg_resources

__version__ = pkg_resources.get_distribution("gleaned").version

# Expose key classes at the package level
from .harvester import DataHarvester
from .datasources.base import DataSource
from .datasources.wmi_source import WMIDataSource