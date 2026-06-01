#!/bin/bash
set -e
cd /mnt/d/gits/simple-a2a-registry-v2/a2a-admin
npm run build 2>&1
echo "BUILD_EXIT=$?"