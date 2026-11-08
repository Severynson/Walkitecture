Harness for the project:

- When you do commits - before that, also write all the changes which were made since the last commit in @CHANGELOG.md.

- If not asked otherwise, If some changes occurred on the branch which you updated locally and tried to push updates to the remote branch, but there is a conflict - fetch changes which were made in the remote repository, then compare them with local changes and try to meaningfully combine all the changes to preserve all the functionality added recently in case if changes doesn't contradict one to another.

- If you are not asked to skip tests, please run relevant tests before considering that certain feature works and is fully implemented properly.
