#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
import subprocess
import time
from os import path, geteuid, makedirs, getcwd, chdir
from shutil import copyfile
from urlparse import urlparse

import argparse

logger = logging.getLogger(__file__)
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
formatter = logging.Formatter('[ %(levelname)s ] %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

nginx_ssl_config_directory = '/etc/nginx/ssl'


class ComposeContext:
    def __init__(self, compose_path):
        self.compose_path = compose_path

    def __enter__(self):
        self.current_path = getcwd()
        chdir(self.compose_path)  # Change to docker-compose file's directory

    def __exit__(self, type, value, traceback):
        chdir(self.current_path)  # Go back


def check_permissions():
    if geteuid() != 0:
        logger.error("Se necesitan permisos de root (sudo).")
        exit(1)


def check_docker():
    subprocess.check_call([
        "docker",
        "ps"
    ])


def check_installdir(base_path):
    if path.isdir(base_path):
        logger.error("Se encontró instalación previa en %s, abortando." % base_path)
        logger.error("El directorio no debería existir.")
        exit(1)
    else:
        makedirs(base_path)


def check_compose():
    subprocess.check_call([
        "docker-compose",
        "--version",
    ])


def download_file(file_path, download_url):
    subprocess.check_call([
        "curl",
        download_url,
        "--fail",
        "--output",
        file_path
    ])


def get_compose_file(base_path, download_url, compose_file, use_local_compose_files):
    parent_directory = os.path.abspath(os.path.join(subprocess.check_output('pwd', shell=True).strip(), os.pardir))
    local_compose_file_path = path.join(parent_directory, compose_file)
    dest_compose_file_path = path.join(base_path, compose_file)
    if use_local_compose_files and os.path.isfile(local_compose_file_path):
        copyfile(local_compose_file_path, dest_compose_file_path)
    else:
        download_file(dest_compose_file_path, download_url)
    return dest_compose_file_path


def get_stable_version_file(base_path, download_url):
    compose_file = "stable_version.yml"
    stable_version_path = path.join(base_path, compose_file)
    download_file(stable_version_path, download_url)
    return stable_version_path


def configure_env_file(base_path, cfg):
    env_file = ".env"
    env_file_path = path.join(base_path, env_file)
    if cfg.andino_version:
        andino_version = cfg.andino_version
    else:
        logger.info("Configurando versión estable de andino.")
        stable_version_path = get_stable_version_file(base_path, stable_version_url)
        with file(stable_version_path, "r") as f:
            content = f.read()
        andino_version = content.strip()
    logger.info("Usando versión '%s' de andino" % andino_version)
    with open(env_file_path, "w") as env_f:
        env_f.write("SITE_HOST=%s\n" % cfg.site_host)
        env_f.write("POSTGRES_USER=%s\n" % cfg.database_user)
        env_f.write("ANDINO_TAG=%s\n" % andino_version)
        env_f.write("POSTGRES_PASSWORD=%s\n" % cfg.database_password)
        env_f.write("NGINX_HOST_PORT=%s\n" % cfg.nginx_port)
        env_f.write("NGINX_HOST_SSL_PORT=%s\n" % cfg.nginx_ssl_port)
        env_f.write("DATASTORE_HOST_PORT=%s\n" % cfg.datastore_port)
        env_f.write("maildomain=%s\n" % cfg.site_host)
        env_f.write("NGINX_CONFIG_FILE=%s\n" % get_nginx_configuration(cfg))
        env_f.write("FILE_SIZE_LIMIT=%s\n" % cfg.file_size_limit)
        # Podría usarse un string que contenga todas las configuraciones extra de Nginx, pero por ahora es innecesario
        env_f.write("NGINX_EXTENDED_CACHE=%s\n" % ("yes" if cfg.nginx_extended_cache else "no"))
        if cfg.nginx_cache_max_size:
            env_f.write("NGINX_CACHE_MAX_SIZE=%s\n" % cfg.nginx_cache_max_size)
        if cfg.nginx_cache_inactive:
            env_f.write("NGINX_CACHE_INACTIVE=%s\n" % cfg.nginx_cache_inactive)
        env_f.write("TZ=%s\n" % cfg.timezone)
        env_f.write("THEME_VOLUME_SRC=%s\n" % cfg.theme_volume_src)


def get_nginx_configuration(cfg):
    if cfg.nginx_ssl:
        if path.isfile(cfg.ssl_crt_path) and path.isfile(cfg.ssl_key_path):
            return "nginx_ssl.conf"
        else:
            logger.error("No se puede utilizar el archivo de configuración para SSL debido a que falta al menos un "
                         "archivo para el certificado. Se utilizará el default en su lugar.")
    return "nginx.conf"


def pull_application(compose_path, dev_compose_path, theme_volume_src):
    extra_file_argument = "-f {}".format(dev_compose_path) if theme_volume_src else ""
    subprocess.check_call(
        ["docker-compose -f {0} {1} pull --ignore-pull-failures".format(compose_path, extra_file_argument)], shell=True)


def init_application(compose_path, dev_compose_path, theme_volume_src):
    extra_file_argument = "-f {}".format(dev_compose_path) if theme_volume_src else ""
    subprocess.check_call(["docker-compose -f {0} {1} up -d nginx".format(compose_path, extra_file_argument)], shell=True)


def configure_application(compose_path, cfg):
    subprocess.check_call([
        "docker-compose",
        "-f",
        compose_path,
        "exec",
        "-T",
        "portal",
        "/etc/ckan_init.d/init.sh",
        "-e", cfg.error_email,
        "-h", cfg.site_host,
        "-p", cfg.database_user,
        "-P", cfg.database_password,
        "-d", cfg.datastore_user,
        "-D", cfg.datastore_password,
    ])


def configure_nginx_extended_cache(compose_path):
    subprocess.check_call([
        "docker-compose",
        "-f",
        compose_path,
        "exec",
        "-T",
        "portal",
        "/etc/ckan_init.d/update_conf.sh",
        "andino.cache_clean_hook=http://nginx/meta/cache/purge",
    ])
    subprocess.check_call([
        "docker-compose",
        "-f",
        compose_path,
        "exec",
        "-T",
        "portal",
        "/etc/ckan_init.d/update_conf.sh",
        "andino.cache_clean_hook_method=PURGE",
    ])


def persist_ssl_certificates(cfg):
    subprocess.check_call([
        "docker",
        "cp",
        cfg.ssl_key_path,
        'andino-nginx:{0}/andino.key'.format(nginx_ssl_config_directory)
    ])
    subprocess.check_call([
        "docker",
        "cp",
        cfg.ssl_crt_path,
        'andino-nginx:{0}/andino.crt'.format(nginx_ssl_config_directory)
    ])


def include_necessary_nginx_configuration(filename):
    subprocess.check_call([
        "docker",
        "exec",
        "-d",
        "andino-nginx",
        "/etc/nginx/scripts/{0}".format(filename)
    ])


def update_site_url_in_configuration_file(cfg, compose_path):
    # Se modifica el campo "ckan.site_url" modificando el protocolo para que quede HTTP o HTTP según corresponda
    current_url = subprocess.check_output(
        'docker-compose -f {} exec -T portal grep -E "^ckan.site_url[[:space:]]*=[[:space:]]*" '
        '/etc/ckan/default/production.ini | tr -d [[:space:]]'.format(compose_path), shell=True).strip()
    current_url = current_url.replace('ckan.site_url', '')[1:]  # guardamos sólo la url, ignoramos el símbolo '='
    host_name = urlparse(current_url).hostname
    is_custom_ssl_port = cfg.nginx_ssl_port != '443' and get_nginx_configuration(cfg) == 'nginx_ssl.conf'
    if is_custom_ssl_port:
        port = cfg.nginx_ssl_port
    elif '80' != cfg.nginx_port:
        port = cfg.nginx_port
    else:
        port = ''
    new_url = "http{0}://{1}{2}".format(
        's' if get_nginx_configuration(cfg) == 'nginx_ssl.conf' else '',
        host_name,
        ':{}'.format(port) if port else '')
    if current_url != new_url:
        subprocess.check_call([
            "docker-compose",
            "-f",
            compose_path,
            "exec",
            "-T",
            "portal",
            "/etc/ckan_init.d/change_site_url.sh",
            new_url,
        ])
    return new_url


def update_config_file_value(value, compose_path):
    if value:
        subprocess.check_call([
            "docker-compose",
            "-f",
            compose_path,
            "exec",
            "-T",
            "portal",
            "/etc/ckan_init.d/update_conf.sh",
            value,
        ])


def ping_nginx_until_200_response_or_timeout(site_url):
    timeout = time.time() + 60 * 5  # límite de 5 minutos
    site_status_code = 0
    while site_status_code != "200":
        site_status_code = subprocess.check_output(
            'echo $(curl -k -s -o /dev/null -w "%{{http_code}}" {})'.format(site_url), shell=True).strip()
        print("Intentando comunicarse con: {0} - Código de respuesta: {1}".format(site_url, site_status_code))
        if time.time() > timeout:
            logger.warning("No fue posible reiniciar el contenedor de Nginx. "
                           "Es posible que haya problemas de configuración.")
            break
        time.sleep(10 if site_status_code != "200" else 0)  # Si falla, esperamos 10 segundos para reintentarlo


def install_andino(cfg, compose_file_url, dev_compose_file_url):
    # Check
    directory = cfg.install_directory
    logger.info("Comprobando permisos (sudo)")
    check_permissions()
    logger.info("Comprobando instalación previa")
    check_installdir(directory)
    logger.info("Comprobando que docker esté instalado...")
    check_docker()
    logger.info("Comprobando que docker-compose esté instalado...")
    check_compose()

    # Download and install
    logger.info("Descargando archivos necesarios...")
    compose_file_path = get_compose_file(directory, compose_file_url, "latest.yml", cfg.use_local_compose_files)
    dev_compose_file_path = get_compose_file(directory, dev_compose_file_url, "latest.dev.yml",
                                             cfg.use_local_compose_files)
    logger.info("Escribiendo archivo de configuración del ambiente (.env) ...")
    configure_env_file(directory, cfg)
    with ComposeContext(directory):
        logger.info("Obteniendo imágenes de Docker")
        pull_application(compose_file_path, dev_compose_file_path, cfg.theme_volume_src)
        # Configure
        logger.info("Iniciando la aplicación")
        init_application(compose_file_path, dev_compose_file_path, cfg.theme_volume_src)
        logger.info("Esperando a que la base de datos este disponible...")
        time.sleep(10)
        if cfg.nginx_extended_cache:
            logger.info("Configurando caché extendida de nginx")
            configure_nginx_extended_cache(compose_file_path)
            include_necessary_nginx_configuration("extend_nginx.sh")
        if cfg.ssl_crt_path and cfg.ssl_key_path:
            logger.info("Copiando archivos del certificado de SSL")
            if path.isfile(cfg.ssl_crt_path) and path.isfile(cfg.ssl_key_path):
                persist_ssl_certificates(cfg)
            else:
                logger.error("No se pudo encontrar al menos uno de los archivos, por lo que no se realizará el copiado")
        logger.info("Configurando...")
        configure_application(compose_file_path, cfg)
        site_url = update_site_url_in_configuration_file(cfg, compose_file_path)
        update_config_file_value("ckan.max_resource_size = {}".format(cfg.file_size_limit), compose_file_path)
        if cfg.theme_volume_src != "/dev/null":
            subprocess.check_call("docker-compose -f latest.yml exec portal /usr/lib/ckan/default/bin/pip install "
                                  "-e /opt/theme",
                                  shell=True)
        subprocess.check_call(["docker-compose", "-f", "latest.yml", "restart", "nginx"])
        logger.info("Esperando a que Nginx se reinicie...")
        ping_nginx_until_200_response_or_timeout(site_url)
        subprocess.check_call("docker-compose -f latest.yml exec portal supervisorctl restart all", shell=True)
        logger.info("Listo.")


def parse_args():
    parser = argparse.ArgumentParser(description='Instalar andino con docker.')

    parser.add_argument('--error_email', required=True)
    parser.add_argument('--site_host', required=True)
    parser.add_argument('--database_user', required=True)
    parser.add_argument('--database_password', required=True)
    parser.add_argument('--datastore_user', required=True)
    parser.add_argument('--datastore_password', required=True)

    parser.add_argument('--andino_version')
    parser.add_argument('--nginx_port', default="80")
    parser.add_argument('--nginx_ssl_port', default="443")
    parser.add_argument('--datastore_port', default="8800")
    parser.add_argument('--branch', default='master')
    parser.add_argument('--install_directory', default='/etc/portal/')
    parser.add_argument('--file_size_limit', default='300')
    parser.add_argument('--nginx-extended-cache', action="store_true")
    parser.add_argument('--nginx-cache-max-size', default="")
    parser.add_argument('--nginx-cache-inactive', default="")
    parser.add_argument('--nginx_ssl', action="store_true")
    parser.add_argument('--ssl_key_path', default="")
    parser.add_argument('--ssl_crt_path', default="")
    parser.add_argument('--timezone', default="America/Argentina/Buenos_Aires")
    parser.add_argument('--use_local_compose_files', action="store_true")
    parser.add_argument('--theme_volume_src', default="/dev/null")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    base_url = "https://raw.githubusercontent.com/datosgobar/portal-andino"
    branch = args.branch
    compose_file_name = "latest.yml"
    dev_compose_file_name = "latest.dev.yml"
    stable_version_file_nane = "stable_version.txt"

    compose_url = path.join(base_url, branch, compose_file_name)
    dev_compose_url = path.join(base_url, branch,  dev_compose_file_name)
    stable_version_url = path.join(base_url, branch, "install", stable_version_file_nane)

    install_andino(args, compose_url, dev_compose_url)
