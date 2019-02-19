#!/usr/bin/env python2.7
import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from textwrap import wrap
from time import sleep

from dnslib import DNSLabel, QTYPE, RR, dns
from dnslib.proxy import ProxyResolver
from dnslib.server import DNSServer
import random
SERIAL_NO = int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds())

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter('%(asctime)s: %(message)s', datefmt='%H:%M:%S'))

logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

TYPE_LOOKUP = {
    'A': (dns.A, QTYPE.A),
    'AAAA': (dns.AAAA, QTYPE.AAAA),
    'CAA': (dns.CAA, QTYPE.CAA),
    'CNAME': (dns.CNAME, QTYPE.CNAME),
    'DNSKEY': (dns.DNSKEY, QTYPE.DNSKEY),
    'MX': (dns.MX, QTYPE.MX),
    'NAPTR': (dns.NAPTR, QTYPE.NAPTR),
    'NS': (dns.NS, QTYPE.NS),
    'PTR': (dns.PTR, QTYPE.PTR),
    'RRSIG': (dns.RRSIG, QTYPE.RRSIG),
    'SOA': (dns.SOA, QTYPE.SOA),
    'SRV': (dns.SRV, QTYPE.SRV),
    'TXT': (dns.TXT, QTYPE.TXT),
    'SPF': (dns.TXT, QTYPE.TXT),
}


class Record:
    def __init__(self, rname, rtype, args):
        self._rname = DNSLabel(rname)

        rd_cls, self._rtype = TYPE_LOOKUP[rtype]

        if self._rtype == QTYPE.SOA and len(args) == 2:
            # add sensible times to SOA
            args += (SERIAL_NO, 3600, 3600 * 3, 3600 * 24, 3600),

        if self._rtype == QTYPE.TXT and len(args) == 1 and isinstance(args[0], str) and len(args[0]) > 255:
            # wrap long TXT records as per dnslib's docs.
            args = wrap(args[0], 255),

        if self._rtype in (QTYPE.NS, QTYPE.SOA):
            ttl = 3600 * 24
        else:
            ttl = 300

        self.rr = RR(
            rname=self._rname,
            rtype=self._rtype,
            rdata=rd_cls(*args),
            ttl=ttl,
        )

    def match(self, q):
        return q.qname == self._rname and (q.qtype == QTYPE.ANY or q.qtype == self._rtype)

    def sub_match(self, q):
        return self._rtype == QTYPE.SOA and q.qname.matchSuffix(self._rname)

    def __str__(self):
        return str(self.rr)


class BadResolver(ProxyResolver):
    def __init__(self, upstream, ip_file, bad_ip_pool_file, bad_probability=0.3):
        super(BadResolver, self).__init__(upstream, 53, 5)
        if os.path.isfile(ip_file):
            self.ips = json.load(file(ip_file))
        else:
            self.ips = {"good": {}, "bad": {}}
        self.ip_file = ip_file
        self.bad_ip_pool = json.load(file(bad_ip_pool_file))
        self.bad_probability = bad_probability

    def update_ip_file(self):
        json.dump(self.ips, file(self.ip_file, "wb"),
                  sort_keys=True, indent=4, separators=(',', ': '))

    def inspect_rdata(self, rdata):
        if type(rdata) in [dns.A, dns.AAAA]:
            ip = repr(rdata)
            if ip in self.ips["good"]:
                return rdata
            elif ip in self.ips["bad"]:
                new_ip = self.ips["bad"][ip]
                return dns.A(new_ip)
            else:
                if random.random() < self.bad_probability:
                    ip_index = random.randrange(0, len(self.bad_ip_pool))
                    new_ip = self.bad_ip_pool[ip_index]
                    new_rdata = dns.A(new_ip)
                    if len(self.ips["bad"]) < len(self.bad_ip_pool):
                        self.ips["bad"][ip] = new_ip
                        self.update_ip_file()
                    return new_rdata
                else:
                    if len(self.ips["bad"]) < len(self.bad_ip_pool):
                        self.ips["good"][ip] = rdata.__class__.__name__  # str(type(rdata)).rsplit(".", 1)[-1]
                        self.update_ip_file()
                        return rdata
                    ip_index = random.randrange(0, len(self.ips["good"]))
                    new_ip = self.ips["good"].keys()[ip_index]
                    rtype = self.ips["good"][new_ip]
                    if rtype == "A":
                        new_rdata = dns.A(new_ip)
                    else:
                        new_rdata = dns.AAAA(new_ip)
                    return new_rdata

    def inspect_rr(self, rr):
        if "ntp" in str(rr.rname).lower():
            rr.rdata = self.inspect_rdata(rr.rdata)

    def resolve(self, request, handler):
        type_name = QTYPE[request.q.qtype]
        replay = super(BadResolver, self).resolve(request, handler)
        print request
        print replay.rr
        print type(replay.rr[0])
        print type(replay.rr[0].rdata)
        print [r.rdata for r in replay.rr]
        map(self.inspect_rr, replay.rr)
        return replay



def handle_sig(signum, frame):
    logger.info('pid=%d, got signal: %s, stopping...', os.getpid(), signal.Signals(signum).name)
    exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_sig)

    port = int(os.getenv('PORT', 53))
    upstream = os.getenv('UPSTREAM', '8.8.8.8')
    ips_file = Path(os.getenv('IPS_FILE', 'ips.json')).as_posix()
    bad_ip_pool_file = Path(os.getenv('BAD_FILE', 'bad_ips_pool.json')).as_posix()
    resolver = BadResolver(upstream, ip_file=ips_file, bad_ip_pool_file=bad_ip_pool_file)
    udp_server = DNSServer(resolver, port=port)
    tcp_server = DNSServer(resolver, port=port, tcp=True)

    logger.info('starting DNS server on port %d, upstream DNS server "%s"', port, upstream)
    udp_server.start_thread()
    tcp_server.start_thread()

    try:
        while udp_server.isAlive():
            sleep(1)
    except KeyboardInterrupt:
        pass