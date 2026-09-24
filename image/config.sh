#!/bin/bash

test -f /.kconfig && . /.kconfig
test -f /.profile && . /.profile

set -euxo pipefail

rm -f /var/lib/zypp/AnonymousUniqueId /var/lib/systemd/random-seed

# clear machine id and regenerate it later on
echo 'uninitialized' > /etc/machine-id
systemctl enable systemd-firstboot

baseSetRunlevel multi-user.target

suseImportBuildKey

systemctl enable sshd.service
systemctl enable systemd-resolved

if [ -x /usr/sbin/firewalld ]; then
        systemctl enable firewalld.service
fi

# Enable NetworkManager if installed
if rpm -q --whatprovides NetworkManager >/dev/null; then
        systemctl enable NetworkManager.service
fi

# Add repos from control.xml
if rpm -q live-add-yast-repos; then
	add-yast-repos
	zypper --non-interactive rm -u live-add-yast-repos
fi

# Enable chrony if installed
if [ -f /etc/chrony.conf ]; then
	systemctl enable chronyd
fi

consoles='console=ttyS0,115200 console=tty0'
cmdline=('rw' 'quiet' 'systemd.show_status=1' ${consoles})
# Configure SELinux if installed
# Note: Because of https://github.com/OSInside/kiwi/issues/2709, the root filesystem
# isn't fully labelled, but the first system snapshot is created after autorelabel
# so this is never visible.
if [[ -e /etc/selinux/config ]]; then
	cmdline+=('security=selinux' 'selinux=1')

	sed -i -e 's|^SELINUX=.*|SELINUX=enforcing|g' \
	       -e 's|^SELINUXTYPE=.*|SELINUXTYPE=targeted|g' \
	       "/etc/selinux/config"
fi

if rpm -q sdbootutil; then
	mkdir -p /etc/kernel
	echo "${cmdline[*]}" > /etc/kernel/cmdline
elif [ -e /etc/default/grub ]; then
	sed -i "s#^GRUB_CMDLINE_LINUX_DEFAULT=.*\$#GRUB_CMDLINE_LINUX_DEFAULT=\"${cmdline[*]}\"#" /etc/default/grub
else
	echo "Unknown bootloader"
	exit 1
fi

# if /etc/zypp/zypp.conf exists, patch it - otherwise rely on packages providing functionality
if [ -f /etc/zypp/zypp.conf ]; then
	#======================================
	# Disable recommends on virtual images (keep hardware supplements, see bsc#1089498)
	#--------------------------------------
	sed -i 's/.*solver.onlyRequires.*/solver.onlyRequires = true/g' /etc/zypp/zypp.conf

	#======================================
	# Disable installing documentation
	#--------------------------------------
	sed -i 's/.*rpm.install.excludedocs.*/rpm.install.excludedocs = yes/g' /etc/zypp/zypp.conf
fi

#
# demo image specific stuff
#

systemctl enable qemu-guest-agent
systemctl enable k3s-node.service
systemctl mask k3s-server.service
systemctl mask k3s-agent.service

rm -rf /var/lib/rancher/k3s /etc/rancher/k3s/config.yaml /var/lib/k3s-kvm-demo
