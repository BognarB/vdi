import math
from subprocess import Popen, PIPE
import re
import socket
from datetime import timedelta, datetime

from django.http import HttpResponse, HttpResponseRedirect
from django.conf import settings

from celery.task import PeriodicTask, Task
from celery.registry import tasks
from celery.decorators import task

from opus.lib import osutils
from opus.lib.ssh_tools import HostNotConnectableError
from opus.lib.log import get_logger

from vdi.models import Application, Instance
from vdi.app_cluster_tools import AppCluster
from vdi import user_experience_tools
from vdi import driver_tools

class ScaleScheduler(PeriodicTask):

    run_every = timedelta(seconds=5)  # Used for tick frequency

    def is_due(self, last_run_at):
        for app in Application.objects.exclude(to_be_run_at__gt=datetime.now()):
            app.to_be_run_at = datetime.now() + timedelta(seconds=app.scale_interarrival)
            app.save()
            Scale.delay(app)
        return (False, self.timedelta_seconds(ScaleScheduler.run_every.run_every))

tasks.register(ScaleScheduler)

class Scale(Task):

    def run(self, app):
        # Create an instance of the logger
        log = get_logger('vdi')

        # Create the cluster object to help us manage the cluster
        cluster = AppCluster(app.pk)

        # Clean up all idle users on all nodes for this application cluster
        log.debug('APP NAME %s'%app.name)
        cluster.logout_idle_users()

        log.debug("Checking for active clusters")
        for node in cluster.active:
            log.debug("Found active host")
            osutil_node = osutils.get_os_object(node.ip, settings.MEDIA_ROOT + str(node.application.ssh_key))
            user_experience_tools.process_user_connections(osutil_node)

        # Handle vms we were waiting on to boot up
        booting = driver_tools.get_instances(cluster.booting)
        for vm in booting:
            dns_name = vm.public_addresses[0]
            log.debug('ASDF = %s' % dns_name)
            if dns_name.find("amazonaws.com") > -1:
                # Resolve the domain name into an IP address
                # This adds a dependancy on the 'host' command
                output = Popen(["host", dns_name], stdout=PIPE).communicate()[0]
                ip = '.'.join(re.findall('(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)', output)[0])
                try:
                    # TODO: remove the hard coded '3389' & '22' below.  '3389' is for RDP and '22' is for SSH
                    # TODO: remove the arbitrary '3' second timeout below
                    socket.create_connection((ip,22),3)
                    #socket.create_connection((ip,3389),3)
                except Exception as e:
                    log.debug("Server %s is not yet available" % ip)
                    pass
                else:
                    instance = Instance.objects.filter(instanceId=vm.id)[0]
                    booting.remove(vm)
                    instance.ip = ip
                    instance.state = 2
                    instance.save()
                    log.debug("Moving instance %s into enabled state with ip %s" % (instance.instanceId,ip))
        num_booting = len(booting)
        if num_booting > 0:
            log.debug("Application cluster '%s' is still waiting for %s cluster nodes to boot" % (cluster.name,len(booting)))


        # Consider if the cluster needs to be scaled
        log.debug('Considering %s app cluster for scaling ...' % cluster.name)
        # Should I scale up?
        log.debug('%s is avail (%s) < req (%s)?' % (cluster.app.name, cluster.avail_headroom, cluster.req_headroom))
        if cluster.avail_headroom < cluster.req_headroom:
            # Yes I should scale up
            space_needed = cluster.req_headroom - cluster.avail_headroom
            servers_needed = int(math.ceil(space_needed / float(cluster.app.users_per_small)))
            log.debug('Available headroom (%s) is less than the cluster headroom goal (%s).  Starting %s additional cluster nodes now' % (cluster.avail_headroom,cluster.req_headroom,servers_needed))
            for i in range(servers_needed):
                cluster.start_node()


        # Handle instances we are supposed to shut down
        toTerminate = []
        for host in cluster.shutting_down:
            log.debug('ASDASDASD    %s' % host.instanceId)
            try:
                osutil_node = osutils.get_os_object(host.ip, settings.MEDIA_ROOT + str(self.host.application.ssh_key))
                log.debug('Node %s is waiting to be shut down and has %s connections' % (host.ip, osutil_node.sessions))
                if osutil_node.sessions == []:
                    toTerminate.append(host)
                    host.shutdownDateTime = datetime.now()
                    host.save()
            except HostNotConnectableError:
                # Ignore this host that doesn't seem to be ssh'able, but log it as an error
                log.warning('Node %s is NOT sshable and should be looked into.  It is currently waiting to shutdown')
        driver_tools.terminate_instances(toTerminate)


        # Should I scale down?
        overprov_num = cluster.avail_headroom - cluster.req_headroom
        log.debug('overprov (%s) avail (%s) required(%s)' % (overprov_num,cluster.avail_headroom,cluster.req_headroom))
        # Reverse the list to try to remove the servers at the end of the waterfall
        inuse_reverse = cluster.inuse_map
        inuse_reverse.reverse()
        for (host,inuse) in inuse_reverse:
            # The node must have 0 sessions and the cluster must be able to be smaller while still leaving enough headroom
            if int(inuse) == 0 and overprov_num >= cluster.app.users_per_small:
                overprov_num = overprov_num - cluster.app.users_per_small
                host.state = 4
                host.save()
                log.debug('Application Server %s has no sessions.  Removing that node from the cluster!' % host.ip)
        return 'scaling complete @TODO put scaling event summary in this output'
