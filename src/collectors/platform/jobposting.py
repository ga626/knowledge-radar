"""静态职位结构化字段；缺字段明确留空，不从导航文字猜薪资。"""
import json
from bs4 import BeautifulSoup
from collectors.platform.job_domain import job_detail_quality


def parse_jobposting(html):
    soup=BeautifulSoup(html,'lxml')
    def entries(value):
        if isinstance(value,list):
            for item in value:yield from entries(item)
        elif isinstance(value,dict):
            kind=value.get('@type',[])
            if kind=='JobPosting' or isinstance(kind,list) and 'JobPosting' in kind:yield value
            if '@graph' in value:yield from entries(value['@graph'])
    for script in soup.select('script[type="application/ld+json"]'):
        try: nodes=list(entries(json.loads(script.get_text())))
        except (ValueError,TypeError):continue
        for job in nodes:
            title=str(job.get('title') or '')
            content=BeautifulSoup(str(job.get('description') or ''),'lxml').get_text('\n',strip=True)
            quality=job_detail_quality(content,extraction_source='jobposting_jsonld',title=title)
            if not quality['ok']:continue
            base=job.get('baseSalary') or {};base=base if isinstance(base,dict) else {}
            value=base.get('value') or {};value=value if isinstance(value,dict) else {'value':value}
            salary=str(value.get('value') or '')
            if value.get('minValue') is not None and value.get('maxValue') is not None:salary=f"{value['minValue']}-{value['maxValue']}"
            if salary:salary+=' '+str(base.get('currency') or '')+' '+str(value.get('unitText') or '')
            employer=job.get('hiringOrganization') or {};employer=employer if isinstance(employer,dict) else {}
            return {'title':title,'content':content,'jd':content,'company':str(employer.get('name') or ''),
                'salary':salary.strip(),'salary_claim_allowed':bool(salary),'detail_quality':quality,
                'missing_fields':[key for key,value in [('company',employer.get('name')),('salary',salary)] if not value],
                'extraction_source':'jobposting_jsonld'}
    return None
