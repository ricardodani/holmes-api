#!/usr/bin/python
# -*- coding: utf-8 -*-

from tornado.gen import coroutine

from holmes.models import Domain
from holmes.handlers import BaseHandler


class DomainsHandler(BaseHandler):

    def get(self):
        domains = self.db.query(Domain).order_by(Domain.name.asc()).all()
        violations_per_domain = Domain.get_violations_per_domain(self.db)
        pages_per_domain = Domain.get_pages_per_domain(self.db)

        if not domains:
            self.write("[]")
            return

        result = []

        for domain in domains:
            result.append({
                "url": domain.url,
                "name": domain.name,
                "violationCount": violations_per_domain.get(domain.id, 0),
                "pageCount": pages_per_domain.get(domain.id, 0)
            })

        self.write_json(result)


class DomainDetailsHandler(BaseHandler):

    @coroutine
    def get(self, domain_name):
        domain = Domain.get_domain_by_name(domain_name, self.db)

        if not domain:
            self.set_status(404, 'Domain %s not found' % domain_name)
            return

        page_count = yield self.cache.get_page_count(domain)
        violation_count = domain.get_violation_data(self.db)

        domain_json = {
            "name": domain.name,
            "url": domain.url,
            "pageCount": page_count,
            "violationCount": violation_count,
        }

        self.write_json(domain_json)


class DomainViolationsPerDayHandler(BaseHandler):

    def get(self, domain_name):
        domain = Domain.get_domain_by_name(domain_name, self.db)

        if not domain:
            self.set_status(404, 'Domain %s not found' % domain_name)
            return

        violations_per_day = domain.get_violations_per_day(self.db)

        domain_json = {
            "name": domain.name,
            "url": domain.url,
            "violations": violations_per_day
        }

        self.write_json(domain_json)


class DomainReviewsHandler(BaseHandler):

    def get(self, domain_name):
        current_page = int(self.get_argument('current_page', 1))
        page_size = 10

        domain = Domain.get_domain_by_name(domain_name, self.db)

        if not domain:
            self.set_status(404, 'Domain %s not found' % domain_name)
            return

        review_count = domain.get_active_review_count(self.db)
        reviews = domain.get_active_reviews(self.db, current_page=current_page, page_size=page_size)

        result = {
            'domainName': domain.name,
            'domainURL': domain.url,
            'pageCount': review_count,
            'pages': [],
        }

        for review in reviews:
            result['pages'].append({
                "url": review.page.url,
                "uuid": str(review.page.uuid),
                "violationCount": len(review.violations),
                "completedAt": review.completed_date,
                "reviewId": str(review.uuid)
            })

        self.write_json(result)
