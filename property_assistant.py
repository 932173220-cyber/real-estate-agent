from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
from real_estate_desktop.cli import launch
if __name__=='__main__':launch()
