from web_watcher.services import ServiceManager
import time
m = ServiceManager()
m.start_all()
time.sleep(300)
