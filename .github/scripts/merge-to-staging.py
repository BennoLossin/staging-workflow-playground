import os
import re
import requests
import subprocess
import sys
import tomllib
import traceback

def cmd(command, env=None):
    current_env = os.environ.copy()
    if env:
        current_env.update(env)

    result = subprocess.run(command, capture_output=True, text=True, env=current_env)
    if result.returncode != 0:
        cmd_text = ' '.join(command)
        stderr = result.stderr.strip()
        raise Exception(f"Command failed: `{cmd_text}`\nError: {stderr}")
    return result.stdout.strip()

class Issue:
    def __init__(self):
        self.token = os.getenv("GITHUB_TOKEN")
        self.pr_num = os.getenv("PR_NUMBER")
        self.repo = os.getenv("REPO_FULL_NAME")
        self.maintainer = os.getenv("MAINTAINER")

    def post_comment(self, text):
        url = f"https://api.github.com/repos/{self.repo}/issues/{self.pr_num}/comments"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json"
        }
        requests.post(url, headers=headers, json={"body": text})

    def load_reviewer_metadata(self):
        if not os.path.exists("reviewers.toml"):
            raise Exception("`reviewers.toml` not found in root directory.")

        with open("reviewers.toml", "rb") as file:
            return tomllib.load(file)

    def get_maintainer(self, reviewers_meta):
        if self.maintainer not in reviewers_meta:
            raise Exception(f"User @{self.maintainer} is not listed in `reviewers.toml`.")
        return reviewers_meta[self.maintainer]

    def setup_git_identity(self, metadata):
        cmd(["git", "config", "user.name", metadata['name']])
        cmd(["git", "config", "user.email", metadata['email']])

    def fetch_pr_metadata(self):
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json"
        }
        api_url = f"https://api.github.com/repos/{self.repo}/pulls/{self.pr_num}"
        return requests.get(api_url, headers=headers).json()

    def fetch_reviews(self, reviewers_meta):
        reviewers = set()
        unknown_reviewers = set()
        api_url = f"https://api.github.com/repos/{self.repo}/pulls/{self.pr_num}/reviews"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json"
        }
        reviews = requests.get(api_url, headers=headers).json()
        for review in reviews:
            if review['state'] == 'APPROVED':
                user = review['user']['login']
                if user in reviewers_meta:
                    reviewers.add(user)
                else:
                    unknown_reviewers.add(f"@{user}")
        
        if unknown_reviewers:
            unknown = ", ".join(list(unknown_reviewers))
            self.post_comment(f"Unknown reviewers not found in `reviewers.toml`: {unknown}.")
            
        return sorted(list(reviewers))

    def create_staging_pr_branch(self):
        branch = f"staging/pr-{self.pr_num}"
        cmd(["git", "checkout", "-b", branch, "origin/staging"])
        cmd(["git", "push", "origin", branch])

    def change_pr_target(self):
        api_url = f"https://api.github.com/repos/{self.repo}/pulls/{self.pr_num}"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json"
        }
        resp = requests.patch(api_url, headers=headers, json={"base": f"staging/pr-{self.pr_num}"})
        if resp.status_code != 200:
            raise Exception(f"Failed to change PR target: {resp.text}")

    def merge_pr_fast_forward(self, pr_data):
        cmd(["git", "fetch", pr_data['head']['repo']['clone_url'], pr_data['head']['ref']])
        cmd(["git", "merge", "--ff-only", "FETCH_HEAD"])
        cmd(["git", "push", "origin", f"staging/pr-{self.pr_num}"])

    def apply_trailers(self, merge_base, pr_url, reviewers, reviewers_meta):
        env = {"GIT_SEQUENCE_EDITOR": "sed -i '/^pick /a break'"}
        cmd(["git", "rebase", "-i", merge_base], env=env)

        trailer_block = ""
        for reviewer in reviewers:
            metadata = reviewers_meta[reviewer]
            trailer_block += f"Reviewed-by: {metadata['name']} <{metadata['email']}>\n"
        trailer_block += f"Link: {pr_url.strip()}"

        while os.path.exists(".git/rebase-merge"):
            msg = cmd(["git", "log", "-1", "--format=%B"]).strip()
            msg += "\n"
            
            # If we don't already have trailers at the end, add an extra newline
            if not re.match(r"^[A-Za-z0-9-]+:\s+.+$", msg.splitlines()[-1]):
                msg += "\n"
            msg += trailer_block

            cmd(["git", "commit", "--amend", "-s", "-m", msg])

            subprocess.run(["git", "rebase", "--continue"], capture_output=True)

    def merge_to_staging_and_push(self, pr_data):
        head_owner = pr_data['head']['user']['login']
        head_ref = pr_data['head']['ref']
        rewritten_head = cmd(["git", "rev-parse", "HEAD"])

        cmd(["git", "checkout", "staging"])

        message = f"Merge pull request #{self.pr_num} from {head_owner}/{head_ref}"
        
        cmd(["git", "merge", "--no-ff", rewritten_head, "-m", message])
        cmd(["git", "push", "origin", "staging"])

    def delete_staging_pr_branch(self):
        cmd(["git", "push", "origin", "--delete", f"staging/pr-{self.pr_num}"])

    def post_success(self, reviewers):
        reviewer_list = ""
        if reviewers:
            reviewer_list = " Added `Reviewed-by`'s for:"
            for reviewer in reviewers:
                reviewer_list += f"\n- {reviewer}"
        self.post_comment(f"Successfully added trailers, merged into `staging`, and preserved PR status.\n{reviewer_list}")

    def run(self):
        try:
            reviewers_meta = self.load_reviewer_metadata()
            maintainer_meta = self.get_maintainer(reviewers_meta)
            self.setup_git_identity(maintainer_meta)
            pr_data = self.fetch_pr_metadata()
            reviewers = self.fetch_reviews(reviewers_meta)

            self.create_staging_pr_branch()
            self.change_pr_target()
            self.merge_pr_fast_forward(pr_data)
            self.apply_trailers("origin/staging", pr_data['html_url'], reviewers, reviewers_meta)
            self.merge_to_staging_and_push(pr_data)

            self.delete_staging_pr_branch()
            self.post_success(reviewers)
            
        except Exception as e:
            trace = traceback.format_exec()
            error_msg = f"Merge unsuccessful:\n```\n{str(e)}\n\n{trace}\n```"
            print(error_msg, file=sys.stderr)
            self.post_comment(error_msg)
            exit(1)

def main():
    Issue().run()

if __name__ == "__main__":
    main()
