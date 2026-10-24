from supabase_api import ClassroomApiHandler

class handler(ClassroomApiHandler):
    def do_POST(self):
        self.path = "/api/public/enroll"
        super().do_POST()
