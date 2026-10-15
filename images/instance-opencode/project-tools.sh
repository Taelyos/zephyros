# The tools of the project (ADR 0030), mounted read-only at /opt/project-tools when the operator
# gives some: their bin directory comes last on the PATH of a login shell too, which starts again
# from the PATH of /etc/profile. A tool runs by its name and never hides a program of the image.
case ":$PATH:" in
  *:/opt/project-tools/bin:*) ;;
  *) PATH="$PATH:/opt/project-tools/bin" ;;
esac
export PATH
