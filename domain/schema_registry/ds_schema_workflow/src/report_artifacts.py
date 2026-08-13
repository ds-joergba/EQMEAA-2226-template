import argparse
import json

import requests

BASE_URL = "https://api.github.com"


def generate_comment(comment, content, state):
    """
    Generate a comment including the content of a file. On success, just the short comment
    is used - the wiki link and report content are only relevant when there is something to
    fix.
    :param comment:
    :param content:
    :param state: One of "success", "failure", "error", "pending".
    :return:
    """
    if state == "success":
        return comment

    return (
        comment + "\n"
        "https://wiki.dentsplysirona.com/pages/viewpage.action"
        "?spaceKey=EQMEAA&title=Schema+Definition" + "\n\n" + content
    )


def create_github_commit_status(content, comment, commit_sha, owner, repo_name, api_key,
                                state, context):
    """
    Sets a commit status (required check) so CI/CD validation gates merging independently
    of PR review approvals.

    :param content: Full report text, printed to the CI log (status descriptions are
                    capped at 140 characters by GitHub, too short for a full report).
    :param comment: Short human-readable description shown on the PR's checks list.
    :param commit_sha: SHA of the commit to attach the status to.
    :param owner: GitHub owner/organization the repository belongs to.
    :param repo_name: GitHub repository name.
    :param api_key: GitHub API token.
    :param state: One of "success", "failure", "error", "pending".
    :param context: Identifier for this check, shown in the PR's checks list.
    """
    print(generate_comment(comment, content or "", state))

    url = f"{BASE_URL}/repos/{owner}/{repo_name}/statuses/{commit_sha}"

    payload = json.dumps(
        {
            "state": state,
            "description": comment[:140],
            "context": context,
        }
    )

    response = requests.request(
        "POST",
        url,
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + api_key,
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 201
    assert response.ok


def find_sticky_comment_id(pull_request_id, owner, repo_name, api_key, marker):
    """
    Looks for a previous comment on the PR carrying the given hidden marker.

    :param pull_request_id: Pull request id (issue number) to search comments on.
    :param owner: GitHub owner/organization the repository belongs to.
    :param repo_name: GitHub repository name.
    :param api_key: GitHub API token.
    :param marker: Hidden marker identifying the comment belonging to a given check context.
    :return: The comment id if found, otherwise None.
    """
    url = f"{BASE_URL}/repos/{owner}/{repo_name}/issues/{pull_request_id}/comments"

    response = requests.request(
        "GET",
        url,
        params={"per_page": 100},
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + api_key,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    assert response.status_code == 200
    assert response.ok

    for existing_comment in response.json():
        if marker in existing_comment["body"]:
            return existing_comment["id"]

    return None


def create_github_pr_comment(content, comment, pull_request_id, owner, repo_name, api_key, context,
                             state):
    """
    Posts a PR comment with the full report content, so the PR author can see failure
    details directly on the PR without needing access to CI logs. Updates the existing
    comment for this check context in place (sticky comment) instead of piling up a new
    comment on every re-run.

    :param content: Full report text included in the comment body.
    :param comment: Short lead-in text for the comment.
    :param pull_request_id: Pull request id (issue number) to comment on.
    :param owner: GitHub owner/organization the repository belongs to.
    :param repo_name: GitHub repository name.
    :param api_key: GitHub API token.
    :param context: Identifier for the check this comment belongs to; used to find/tag
                    the sticky comment.
    :param state: One of "success", "failure", "error", "pending".
    """
    marker = f"<!-- report-artifacts:{context} -->"
    body = marker + "\n" + generate_comment(comment, content or "", state)

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + api_key,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    existing_comment_id = find_sticky_comment_id(pull_request_id, owner, repo_name, api_key, marker)

    if existing_comment_id:
        method, url = "PATCH", f"{BASE_URL}/repos/{owner}/{repo_name}/issues/comments/{existing_comment_id}"
    else:
        method, url = "POST", f"{BASE_URL}/repos/{owner}/{repo_name}/issues/{pull_request_id}/comments"

    response = requests.request(method, url, data=json.dumps({"body": body}), headers=headers)

    assert response.status_code in (200, 201)
    assert response.ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Report a CI/CD status check on a commit")
    parser.add_argument("--content", help="Full report text, printed to CI logs")
    parser.add_argument("--comment", help="Short status description shown on the PR")
    parser.add_argument("--commit_sha", help="SHA of the commit to attach the status to")
    parser.add_argument(
        "--pull_request_id",
        default=None,
        help="Pull request id; if provided, a comment with the full report is also posted",
    )
    parser.add_argument("--project_name", dest="owner",
                        help="GitHub owner/organization the repository belongs to")
    parser.add_argument("--git_repo_name", dest="repo_name", help="GitHub repository name")
    parser.add_argument("--api_key", help="GitHub API token")
    parser.add_argument("--state", default="failure",
                        help="Status state: success, failure, error, pending")
    parser.add_argument("--context", default="ci/schema-workflow", help="Status check context name")
    args = parser.parse_args()

    create_github_commit_status(
        content=args.content,
        comment=args.comment,
        commit_sha=args.commit_sha,
        owner=args.owner,
        repo_name=args.repo_name,
        api_key=args.api_key,
        state=args.state,
        context=args.context,
    )

    if args.pull_request_id:
        create_github_pr_comment(
            content=args.content,
            comment=args.comment,
            pull_request_id=args.pull_request_id,
            owner=args.owner,
            repo_name=args.repo_name,
            api_key=args.api_key,
            context=args.context,
            state=args.state,
        )
