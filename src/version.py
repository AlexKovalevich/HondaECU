# import git

def GetVersion():
#    r = git.repo.Repo(search_parent_directories=True)
#    return r.git.describe("--tags")
     return "V-1-AL-DEMO"
__VERSION__ = GetVersion()
