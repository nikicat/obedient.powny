import os
import yaml
from pkg_resources import resource_stream
from dominator.utils import settings, aslist
from dominator.entities import *


def namespace():
    return os.environ.get('OBEDIENT_GNS_NAMESPACE', 'yandex')


def builder(
        zookeepers,
        smtp_server="smtp.example.com",
        smtp_port=25,
        golem_url_ro="http://example.com",
        golem_url_rw="http://example.com",
        threads=10,
        elasticsearch_url='http://elasticlog.yandex.net:9200',
        restapi_port=7887,
        gitapi_port=2022,
        ssh_key='~/.ssh/id_rsa.pub',
    ):

    logging_config = yaml.load(resource_stream(__name__, 'logging.yaml'))
    logging_config['handlers']['elasticsearch']['url'] = elasticsearch_url

    rules = DataVolume(
        dest='/var/lib/gns/rules',
        path='/var/lib/gns/rules',
    )

    def stoppable(cmd):
        return 'trap exit TERM; {} & wait'.format(cmd)

    parent = Image('yandex/trusty')

    gnsimage = SourceImage(
        name='gns',
        parent=parent,
        env={
            'PATH': '$PATH:/opt/pypy3/bin',
            'LANG': 'C.UTF-8',
        },
        scripts=[
            'curl http://buildbot.pypy.org/nightly/py3k/pypy-c-jit-latest-linux64.tar.bz2 2>/dev/null | tar -jxf -',
            'mv pypy* /opt/pypy3',
            'curl https://bitbucket.org/pypa/setuptools/raw/bootstrap/ez_setup.py 2>/dev/null | pypy',
            'easy_install pip==1.4.1',
            'pip install gns==0.2',
        ],
        volumes={
            'config': '/etc/gns',
            'rules': '/var/lib/gns/rules',
            'logs': '/var/log/gns',
        },
        command=stoppable('gns $GNS_MODULE -c /etc/gns/gns.yaml'),
    )
    gnsapiimage = SourceImage(
        name='gns-cpython',
        parent=parent,
        env={'LANG': 'C.UTF-8'},
        scripts=[
            'apt-add-repository ppa:fkrull/deadsnakes -y',
            'apt-get update',
            'apt-get install python3-pip -yy',
            'pip3 install gns==0.2',
            'pip3 install uwsgi',
        ],
        volumes={
            'config': '/etc/gns',
            'rules': '/var/lib/gns/rules',
            'logs': '/var/log/gns',
        },
        command=stoppable('uwsgi --ini /etc/uwsgi/uwsgi.ini'),
    )

    gitapiimage = SourceImage(
        name='gitsplit',
        parent=parent,
        files={
            '/post-receive': resource_stream(__name__, 'post-receive'),
            '/etc/ssh/sshd_config': resource_stream(__name__, 'sshd_config'),
            '/root/run.sh': resource_stream(__name__, 'run.sh'),
        },
        ports={'ssh': 22},
        volumes={
            'rules': '/var/lib/gns/rules',
            'rules.git': '/var/lib/gns/rules.git',
        },
        command='/root/run.sh',
        scripts=[
            'apt-get install -y openssh-server',
            'useradd --non-unique --uid 0 --system --shell /usr/bin/git-shell -d / git',
            'mkdir /run/sshd',
            'chmod 0755 /run/sshd',
        ],
    )

    def make_config():
        return {
            'core': {'zoo-nodes': ['{}:{}'.format(z.ship.fqdn, z.ports['client']) for z in zookeepers]},
            'logging': logging_config,
        }

    def add_service(config, name):
        config[name] = {
            'workers': threads,
            'die-after': None,
        }

    def add_rules(config):
        config['core']['import-alias'] = 'rules'
        config['core']['rules-dir'] = rules.dest

    def add_output(config, email_from):
        config['golem'] = {'url-ro': golem_url_ro, 'url-rw': golem_url_rw}
        config['output'] = {
            'email': {
                'from': email_from,
                'server': smtp_server,
                'port': smtp_port,
            },
        }

    def container(ship, name, config, backdoor=None, ports={}, volumes={}, memory=1024**3, image=gnsimage, files=None):
        if backdoor is not None:
            config['backdoor'] = {'enabled': True, 'port': backdoor}
            ports['backdoor'] = backdoor
        if files is None:
            files = {'gns.yaml': YamlFile(config)}

        _volumes = {'config': ConfigVolume(dest='/etc/gns', files=files)}
        _volumes.update(volumes)

        return Container(
            name='gns-'+name,
            ship=ship,
            image=image,
            memory=memory,
            volumes=_volumes,
            env={'GNS_MODULE': name},
            ports=ports,
        )

    class Builder:
        @staticmethod
        def splitter(ship):
            config = make_config()
            add_service(config, 'splitter')
            add_rules(config)
            return container(ship, 'splitter', config, volumes={'rules': rules}, backdoor=11002, ports={})

        @staticmethod
        def worker(ship):
            config = make_config()
            add_service(config, 'worker')
            add_rules(config)
            add_output(config, 'gns@'+ship.fqdn)
            return container(ship, 'worker', config, volumes={'rules': rules}, backdoor=11001, ports={})

        @staticmethod
        def restapi(ship):
            config = make_config()
            uwsgi_conf = ConfigVolume(dest='/etc/uwsgi', files={'uwsgi.ini': TemplateFile(TextFile('uwsgi.ini'))})
            return container(ship, 'api', config, volumes={'uwsgi-conf': uwsgi_conf},
                             backdoor=None, ports={'http': restapi_port}, image=gnsapiimage)

        @staticmethod
        def collector(ship):
            config = make_config()
            add_service(config, 'collector')
            return container(ship, 'collector', config, backdoor=11003, ports={})

        @staticmethod
        def gitapi(ship):
            rulesgit = DataVolume(
                dest='/var/lib/gns/rules.git',
                path='/var/lib/gns/rules.git',
            )

            return Container(
                name='gitapi',
                ship=ship,
                image=gitapiimage,
                memory=128*1024*1024,
                volumes={'rules.git': rulesgit, 'rules': rules},
                ports=gitapiimage.ports,
                extports={'ssh': gitapi_port},
                env={'KEY': open(ssh_key).read()}
            )

        @staticmethod
        def reinit(ship):
            return container(ship, 'reinit', make_config())


        @classmethod
        @aslist
        def build(cls, ships):
            for ship in ships:
                yield cls.worker(ship)
                yield cls.splitter(ship)
                yield cls.collector(ship)
                yield cls.restapi(ship)
                yield cls.gitapi(ship)

    return Builder