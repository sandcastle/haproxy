import os
import logging
import subprocess
import time
import copy
import re
from collections import OrderedDict

import tutum

from parser import Specs

logger = logging.getLogger("haproxy")


class Haproxy(object):
    # envvar
    envvar_default_ssl_cert = os.getenv("DEFAULT_SSL_CERT") or os.getenv("SSL_CERT")
    envvar_default_ca_cert = os.getenv("CA_CERT")
    envvar_maxconn = os.getenv("MAXCONN", "4096")
    envvar_mode = os.getenv("MODE", "http")
    envvar_option = os.getenv("OPTION", "redispatch, httplog, dontlognull, forwardfor").split(",")
    envvar_rsyslog_destnation = os.getenv("RSYSLOG_DESTINATION", "127.0.0.1")
    envvar_ssl_bind_ciphers = os.getenv("SSL_BIND_CIPHERS")
    envvar_ssl_bind_options = os.getenv("SSL_BIND_OPTIONS")
    envvar_stats_auth = os.getenv("STATS_AUTH", "stats:stats")
    envvar_stats_port = os.getenv("STATS_PORT", "1936")
    envvar_timeout = os.getenv("TIMEOUT", "connect 5000, client 50000, server 50000").split(",")
    envvar_health_check = os.getenv("HEALTH_CHECK", "check inter 2000 rise 2 fall 3")
    envvar_extra_global_settings = os.getenv("EXTRA_GLOBAL_SETTINGS")
    envvar_extra_default_settings = os.getenv("EXTRA_DEFAULT_SETTINGS")
    envvar_http_basic_auth = os.getenv("HTTP_BASIC_AUTH")
    envvar_debug = os.getenv("DEBUG", False)

    # envvar overwritable
    envvar_balance = os.getenv("BALANCE", "roundrobin")

    # const var
    const_cert_dir = "/certs/"
    const_cacert_dir = "/cacerts/"
    const_config_file = "/haproxy.cfg"
    const_command = ['/usr/sbin/haproxy', '-f', const_config_file, '-db', '-q']
    const_api_retry = 10  # seconds

    # class var
    cls_container_uri = os.getenv("TUTUM_CONTAINER_API_URI")
    cls_service_uri = os.getenv("TUTUM_SERVICE_API_URI")
    cls_tutum_auth = os.getenv("TUTUM_AUTH")
    cls_linked_services = None
    cls_cfg = None
    cls_haproxy_process = None
    cls_certs = []

    def __init__(self):
        self.ssl = None
        self.ssl_updated = False
        self.routes_added = []
        self.require_default_route = False
        if Haproxy.cls_container_uri and Haproxy.cls_service_uri and Haproxy.cls_tutum_auth:
            logger.info("Loading HAProxy definition through REST API")
            container = self.fetch_tutum_obj(Haproxy.cls_container_uri)
            service = self.fetch_tutum_obj(Haproxy.cls_service_uri)
            Haproxy.cls_linked_services = [srv.get("to_service") for srv in service.linked_to_service]
            self.specs = Specs(container, service)
        else:
            logger.info("Loading HAProxy definition from environment variables")
            Haproxy.cls_linked_services = None
            Haproxy.specs = Specs()

    def update(self):
        cfg_dict = OrderedDict()
        self._config_ssl()
        cfg_dict.update(self._config_global_defaults())
        for cfg in self._config_tcp():
            cfg_dict.update(cfg)
        cfg_dict.update(self._config_frontend())
        cfg_dict.update(self._config_backend())

        cfg = self._prettify(cfg_dict)
        if Haproxy.cls_service_uri and Haproxy.cls_container_uri and Haproxy.cls_tutum_auth:
            if Haproxy.cls_cfg != cfg:
                if not Haproxy.cls_cfg:
                    logger.info("HAProxy configuration:\n%s" % cfg)
                else:
                    logger.info("HAProxy configuration is updated:\n%s" % cfg)
                Haproxy.cls_cfg = cfg
                if self._save_conf():
                    self._run()
            elif self.ssl_updated:
                self._run()
            else:
                logger.info("HAProxy configuration remains unchanged")
        else:
            logger.info("HAProxy configuration:\n%s" % cfg)
            Haproxy.cls_cfg = cfg
            self._save_conf()
            logger.info("Launching HAProxy")
            p = subprocess.Popen(self.const_command)
            p.wait()

    def _run(self):
        if Haproxy.cls_haproxy_process:
            # Reload haproxy
            logger.info("Reloading HAProxy")
            process = subprocess.Popen(self.const_command + ["-sf", str(Haproxy.cls_haproxy_process.pid)])
            Haproxy.cls_haproxy_process.wait()
            Haproxy.cls_haproxy_process = process
            logger.info("HAProxy has been reloaded\n******************************")
        else:
            # Launch haproxy
            logger.info("Launching HAProxy\n******************************")
            Haproxy.cls_haproxy_process = subprocess.Popen(self.const_command)

    @staticmethod
    def _prettify(cfg):
        text = ""
        for section, contents in cfg.items():
            text += "%s\n" % section
            for content in contents:
                text += "  %s\n" % content
        return text.strip()

    def _config_ssl(self):
        certs = []
        cacerts = []
        if self.envvar_default_ssl_cert:
            certs.append(self.envvar_default_ssl_cert)
        if self.envvar_default_ca_cert:
            cacerts.append(self.envvar_default_ca_cert)
        certs.extend(self.specs.get_default_ssl_cert())
        certs.extend(self.specs.get_ssl_cert())
        if certs:
            if set(certs) != set(Haproxy.cls_certs):
                Haproxy.cls_certs = copy.copy(certs)
                self.ssl_updated = True
                self._save_certs(certs)
            self.ssl = "ssl crt /certs/"
        if cacerts:
            if set(cacerts) != set(Haproxy.cls_certs):
                Haproxy.cls_certs = copy.copy(cacerts)
                self.ssl_updated = True
                self._save_ca_certs(cacerts)
            self.ssl += " ca-file /cacerts/cert0.pem verify required"

    def _save_certs(self, certs):
        try:
            if not os.path.exists(self.const_cert_dir):
                os.makedirs(self.const_cert_dir)
        except Exception as e:
            logger.error(e)
        for index, cert in enumerate(certs):
            cert_filename = "%scert%d.pem" % (self.const_cert_dir, index)
            try:
                with open(cert_filename, 'w') as f:
                    f.write(cert.replace("\\n", '\n'))
            except Exception as e:
                logger.error(e)
        logger.info("SSL certificates are updated")

    def _save_ca_certs(self, certs):
        try:
            if not os.path.exists(self.const_cacert_dir):
                os.makedirs(self.const_cacert_dir)
        except Exception as e:
            logger.error(e)
        for index, cert in enumerate(certs):
            cert_filename = "%scert%d.pem" % (self.const_cacert_dir, index)
            try:
                with open(cert_filename, 'w') as f:
                    f.write(cert.replace("\\n", '\n'))
            except Exception as e:
                logger.error(e)
        logger.info("CA certificates are updated")

    def _save_conf(self):
        try:
            with open(self.const_config_file, 'w') as f:
                f.write(Haproxy.cls_cfg)
            return True
        except Exception as e:
            logger.error(e)
            return False

    @classmethod
    def _config_global_defaults(cls):
        cfg = OrderedDict()
        cfg["global"] = ["log %s local0" % cls.envvar_rsyslog_destnation,
                         "log %s local1 notice" % cls.envvar_rsyslog_destnation,
                         "log-send-hostname",
                         "maxconn %s" % cls.envvar_maxconn,
                         "pidfile /var/run/haproxy.pid",
                         "user haproxy",
                         "group haproxy",
                         "daemon",
                         "stats socket /var/run/haproxy.stats level admin"]
        cfg["defaults"] = ["balance %s" % cls.envvar_balance,
                           "log global",
                           "mode %s" % cls.envvar_mode]
        cfg["listen stats"] = ["bind :%s" % cls.envvar_stats_port,
                               "mode http",
                               "stats enable",
                               "timeout connect 10s",
                               "timeout client 1m",
                               "timeout server 1m",
                               "stats hide-version",
                               "stats realm Haproxy\ Statistics",
                               "stats uri /",
                               "stats auth %s" % cls.envvar_stats_auth]

        if cls.envvar_debug:
            cfg["global"].append("debug")

        for opt in cls.envvar_option:
            if opt:
                cfg["defaults"].append("option %s" % opt.strip())
        for t in cls.envvar_timeout:
            if t:
                cfg["defaults"].append("timeout %s" % t.strip())

        if cls.envvar_ssl_bind_options:
            cfg["global"].append("ssl-default-bind-options %s" % cls.envvar_ssl_bind_options)
        if cls.envvar_ssl_bind_ciphers:
            cfg["global"].append("ssl-default-bind-ciphers %s" % cls.envvar_ssl_bind_ciphers)

        if Haproxy.envvar_extra_default_settings:
            settings = re.split(r'(?<!\\),', Haproxy.envvar_extra_default_settings)
            for setting in settings:
                if setting.strip():
                    cfg["defaults"].append(setting.strip().replace("\,", ","))

        if Haproxy.envvar_extra_global_settings:
            settings = re.split(r'(?<!\\),', Haproxy.envvar_extra_global_settings)
            for setting in settings:
                if setting.strip():
                    cfg["global"].append(setting.strip().replace("\,", ","))

        if Haproxy.envvar_http_basic_auth:
            auth_list = re.split(r'(?<!\\),', Haproxy.envvar_http_basic_auth)
            userlist = []
            for auth in auth_list:
                if auth.strip():
                    terms = auth.strip().split(":", 1)
                    if len(terms) == 2:
                        username = terms[0].replace("\,", ",")
                        password = terms[1].replace("\,", ",")
                        userlist.append("user %s insecure-password %s" % (username, password))

            if userlist:
                cfg["userlist haproxy_userlist"] = userlist
        return cfg

    def _config_tcp(self):
        cfgs = []
        if not self._get_service_attr("tcp_ports"):
            return cfgs

        ports = []
        for service_alias in self.specs.service_aliases:
            tcp_ports = self._get_service_attr("tcp_ports", service_alias)
            if tcp_ports:
                ports.extend(tcp_ports)

        for port in set(ports):
            cfg = OrderedDict()

            ssl = False
            port_num = port
            if port.lower().endswith("/ssl"):
                port_num = port[:-4]
                if self.ssl:
                    ssl = True

            if ssl:
                listen = ["bind :%s %s" % (port_num, self.ssl), "mode tcp"]
            else:
                listen = ["bind :%s" % port, "mode tcp"]

            for _service_alias, routes in self.specs.get_routes().iteritems():
                tcp_ports = self._get_service_attr("tcp_ports", _service_alias)
                if tcp_ports and port in tcp_ports:
                    for route in routes:
                        if route["port"] == port_num:
                            tcp_route = ["server %s %s:%s" % (route["container_name"], route["addr"], route["port"])]

                            health_check = self._get_service_attr("health_check", _service_alias)
                            health_check = health_check if health_check else Haproxy.envvar_health_check
                            tcp_route.append(health_check)

                            listen.append(" ".join(tcp_route))
                            self.routes_added.append(route)

            options = self._get_service_attr('option', service_alias)
            if options:
                for option in options:
                    listen.append("option %s" % option)

            extra_settings = self._get_service_attr('extra_settings', service_alias)
            if extra_settings:
                settings = re.split(r'(?<!\\),', extra_settings)
                for setting in settings:
                    if setting.strip():
                        listen.append(setting.strip().replace("\,", ","))

            cfg["listen port_%s" % port_num] = listen
            cfgs.append(cfg)

        return cfgs

    def _config_frontend(self):
        cfg = OrderedDict()
        if self.specs.get_vhosts():
            frontends_dict = {}
            rule_counter = 0
            for vhost in self.specs.get_vhosts():
                rule_counter += 1
                port = vhost["port"]

                # initialize bind clause for each port
                if port not in frontends_dict:
                    ssl = False
                    for v in self.specs.get_vhosts():
                        if v["port"] == port:
                            scheme = v["scheme"].lower()
                            if scheme in ["https", "wss"] and self.ssl:
                                ssl = True
                                break
                    if ssl:
                        frontends_dict[port] = ["bind :%s %s" % (port, self.ssl), "reqadd X-Forwarded-Proto:\ https"]
                    else:
                        frontends_dict[port] = ["bind :%s" % port]

                    # add websocket acl rule
                    frontends_dict[port].append("acl is_websocket hdr(Upgrade) -i WebSocket")

                acl_rule = []
                # calculate virtual host rule
                host_rules = []
                host = vhost["host"].strip("/")
                if host == "*":
                    pass
                elif "*" in host:
                    host_rules.append("acl host_rule_%d hdr_reg(host) -i %s" % (
                        rule_counter, "^%s$" % host.replace(".", "\.").replace("*", ".*")))
                elif host:
                    host_rules.append("acl host_rule_%d hdr(host) -i %s" % (rule_counter, host))
                    host_rules.append("acl host_rule_%d_port hdr(host) -i %s" % (rule_counter, "%s:%s" % (host, port)))
                acl_rule.extend(host_rules)

                # calculate virtual path rules
                path_rules = []
                path = vhost["path"].strip()
                if "*" in path:
                    path_rules.append(
                        "acl path_rule_%d path_reg -i ^%s$" % (
                        rule_counter, path.replace(".", "\.").replace("*", ".*")))
                elif path:
                    path_rules.append("acl path_rule_%d path -i %s" % (rule_counter, path))
                acl_rule.extend(path_rules)

                if vhost["scheme"].lower() in ["ws", "wss"]:
                    acl_condition = "is_websocket"
                else:
                    acl_condition = ""

                if path_rules:
                    acl_condition = " ".join([acl_condition, "path_rule_%d" % rule_counter])

                if host_rules:
                    if '*' in host:
                        acl_condition = ("%s host_rule_%d" % (acl_condition, rule_counter)).strip()
                    else:
                        acl_condition_1 = ("%s host_rule_%d" % (acl_condition, rule_counter)).strip()
                        acl_condition_2 = ("%s host_rule_%d_port" % (acl_condition, rule_counter)).strip()
                        acl_condition = " or ".join([acl_condition_1, acl_condition_2])

                if acl_condition:
                    use_backend = "use_backend SERVICE_%s if %s" % (vhost["service_alias"], acl_condition)
                    acl_rule.append(use_backend)
                    frontends_dict[port].extend(acl_rule)

            for port, frontend in frontends_dict.iteritems():
                cfg["frontend port_%s" % port] = frontend

        else:
            all_routes = []
            for routes in self.specs.get_routes().itervalues():
                all_routes.extend(routes)
            if len(self.routes_added) < len(all_routes):
                self.require_default_route = True

            if self.require_default_route:
                frontend = ["bind :80"]
                if self.ssl and self:
                    frontend.append("bind :443 %s" % self.ssl)
                    frontend.append("reqadd X-Forwarded-Proto:\ https")
                frontend.append("default_backend default_service")
                cfg["frontend default_frontend"] = frontend

        return cfg

    def _config_backend(self):
        cfg = OrderedDict()

        if not self.specs.get_vhosts():
            services_aliases = [None]
        else:
            services_aliases = self.specs.service_aliases

        for service_alias in services_aliases:
            backend = []
            is_sticky = False

            # Add http-service-close option for websocket backend
            for v in self.specs.get_vhosts():
                if service_alias == v["service_alias"]:
                    if v["scheme"].lower() in ["ws", "wss"]:
                        backend.append("option http-server-close")
                        break

            # To add an entry to backend section: append to backend
            # To add items to a route: append to route_setting
            balance = self._get_service_attr("balance", service_alias)
            if balance:
                backend.append("balance %s" % balance)

            appsession = self._get_service_attr("appsession", service_alias)
            if appsession:
                backend.append("appsession %s" % appsession)
                is_sticky = True

            cookie = self._get_service_attr("cookie", service_alias)
            if cookie:
                backend.append("cookie %s" % cookie)
                is_sticky = True

            force_ssl = self._get_service_attr("force_ssl", service_alias)
            if force_ssl:
                backend.append("redirect scheme https code 301 if !{ ssl_fc }")

            http_check = self._get_service_attr("http_check", service_alias)
            if http_check:
                backend.append("option httpchk %s" % http_check)

            hsts_max_age = self._get_service_attr("hsts_max_age", service_alias)
            if hsts_max_age:
                backend.append("rspadd Strict-Transport-Security:\ max-age=%s;\ includeSubDomains" % hsts_max_age)

            gzip_compression_type = self._get_service_attr('gzip_compression_type', service_alias)
            if gzip_compression_type:
                backend.append("compression algo gzip")
                backend.append("compression type %s" % gzip_compression_type)

            options = self._get_service_attr('option', service_alias)
            if options:
                for option in options:
                    backend.append("option %s" % option)

            extra_settings = self._get_service_attr('extra_settings', service_alias)
            if extra_settings:
                settings = re.split(r'(?<!\\),', extra_settings)
                for setting in settings:
                    if setting.strip():
                        backend.append(setting.strip().replace("\,", ","))

            if Haproxy.envvar_http_basic_auth:
                backend.append("acl need_auth http_auth(haproxy_userlist)")
                backend.append("http-request auth realm haproxy_basic_auth if !need_auth")

            for _service_alias, routes in self.specs.get_routes().iteritems():
                if not service_alias or _service_alias == service_alias:
                    for route in routes:
                        # avoid adding those tcp routes adding http backends
                        if route in self.routes_added:
                            continue

                        backend_route = ["server %s %s:%s" % (route["container_name"], route["addr"], route["port"])]
                        if is_sticky:
                            backend_route.append("cookie %s" % route["container_name"])

                        health_check = self._get_service_attr("health_check", service_alias)
                        health_check = health_check if health_check else Haproxy.envvar_health_check
                        backend_route.append(health_check)

                        backend.append(" ".join(backend_route))

            if not service_alias:
                if self.require_default_route:
                    cfg["backend default_service"] = sorted(backend)
            else:
                if self._get_service_attr("virtual_host", service_alias):
                    cfg["backend SERVICE_%s" % service_alias] = sorted(backend)
                else:
                    cfg["backend default_service"] = sorted(backend)
        return cfg

    def _get_service_attr(self, attr_name, service_alias=None):
        # service is None, when there is no virtual host is set
        if service_alias:
            try:
                return self.specs.get_details()[service_alias][attr_name]
            except:
                return None

        else:
            # Randomly pick a None value from the linked service
            for _service_alias in self.specs.get_details().iterkeys():
                if self.specs.get_details()[_service_alias][attr_name]:
                    return self.specs.get_details()[_service_alias][attr_name]
            return None

    @classmethod
    def fetch_tutum_obj(cls, uri):
        if not uri:
            return None

        while True:
            try:
                obj = tutum.Utils.fetch_by_resource_uri(uri)
                break
            except Exception as e:
                logger.error(e)
                time.sleep(cls.const_api_retry)
        return obj
