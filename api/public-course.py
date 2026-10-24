from supabase_api import ClassroomApiHandler
from urllib.parse import urlsplit

class handler(ClassroomApiHandler):
    def do_GET(self):
        query = urlsplit(self.path).query
        self.path = "/api/public/course" + (f"?{query}" if query else "")
        super().do_GET()
