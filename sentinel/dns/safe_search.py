"""Exact DNS hosts for Google's SafeSearch and YouTube Moderate endpoints.

Google country domains: https://www.google.com/supported_domains
SafeSearch: https://support.google.com/websearch/answer/186669
YouTube: https://support.google.com/a/answer/6214622
Only Google search hosts are rewritten, never *.google.com (accounts/mail/etc.).
"""

GOOGLE_SEARCH_DOMAINS = """
google.com google.ad google.ae google.com.af google.com.ag google.al google.am
google.co.ao google.com.ar google.as google.at google.com.au google.az google.ba
google.com.bd google.be google.bf google.bg google.com.bh google.bi google.bj
google.com.bn google.com.bo google.com.br google.bs google.bt google.co.bw
google.by google.com.bz google.ca google.cd google.cf google.cg google.ch
google.ci google.co.ck google.cl google.cm google.cn google.com.co google.co.cr
google.com.cu google.cv google.com.cy google.cz google.de google.dj google.dk
google.dm google.com.do google.dz google.com.ec google.ee google.com.eg google.es
google.com.et google.fi google.com.fj google.fm google.fr google.ga google.ge
google.gg google.com.gh google.com.gi google.gl google.gm google.gr google.com.gt
google.gy google.com.hk google.hn google.hr google.ht google.hu google.co.id
google.ie google.co.il google.im google.co.in google.iq google.is google.it
google.je google.com.jm google.jo google.co.jp google.co.ke google.com.kh
google.ki google.kg google.co.kr google.com.kw google.kz google.la google.com.lb
google.li google.lk google.co.ls google.lt google.lu google.lv google.com.ly
google.co.ma google.md google.me google.mg google.mk google.ml google.com.mm
google.mn google.com.mt google.mu google.mv google.mw google.com.mx google.com.my
google.co.mz google.com.na google.com.ng google.com.ni google.ne google.nl
google.no google.com.np google.nr google.nu google.co.nz google.com.om
google.com.pa google.com.pe google.com.pg google.com.ph google.com.pk google.pl
google.pn google.com.pr google.ps google.pt google.com.py google.com.qa google.ro
google.ru google.rw google.com.sa google.com.sb google.sc google.se google.com.sg
google.sh google.si google.sk google.com.sl google.sn google.so google.sm
google.sr google.st google.com.sv google.td google.tg google.co.th google.com.tj
google.tl google.tm google.tn google.to google.com.tr google.tt google.com.tw
google.co.tz google.com.ua google.co.ug google.co.uk google.com.uy google.co.uz
google.com.vc google.co.ve google.co.vi google.com.vn google.vu google.ws
google.rs google.co.za google.co.zm google.co.zw google.cat
""".split()

GOOGLE_SEARCH_HOSTS = tuple(sorted({host for domain in GOOGLE_SEARCH_DOMAINS
                                    for host in (domain, f"www.{domain}", f"images.{domain}")}))
# Google explicitly warns against rewriting youtube.com, youtu.be, ytimg, or
# all of googleapis.com. Keep this list exact and do not rewrite the target.
YOUTUBE_RESTRICTED_HOSTS = (
    "www.youtube.com", "m.youtube.com", "youtubei.googleapis.com",
    "youtube.googleapis.com", "www.youtube-nocookie.com",
)
SAFE_SEARCH_REWRITES = {
    **dict.fromkeys(GOOGLE_SEARCH_HOSTS, "forcesafesearch.google.com"),
    **dict.fromkeys(YOUTUBE_RESTRICTED_HOSTS, "restrictmoderate.youtube.com"),
}


def matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")
