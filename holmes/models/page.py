#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys
from uuid import uuid4
from datetime import datetime
import hashlib
import logging

import sqlalchemy as sa
from sqlalchemy.orm import relationship
from sqlalchemy import or_
from ujson import dumps
from tornado.concurrent import return_future

from holmes.models import Base
from holmes.utils import get_domain_from_url


class Page(Base):
    __tablename__ = "pages"

    id = sa.Column(sa.Integer, primary_key=True)
    url = sa.Column('url', sa.String(2000), nullable=False)
    url_hash = sa.Column('url_hash', sa.String(128), nullable=False)
    uuid = sa.Column('uuid', sa.String(36), default=uuid4, nullable=False)
    created_date = sa.Column('created_date', sa.DateTime, default=datetime.utcnow, nullable=False)

    domain_id = sa.Column('domain_id', sa.Integer, sa.ForeignKey('domains.id'))

    reviews = relationship("Review", backref="page", foreign_keys='[Review.page_id]')

    last_review_id = sa.Column('last_review_id', sa.Integer, sa.ForeignKey('reviews.id'))
    last_review = relationship("Review", foreign_keys=[last_review_id])
    last_review_date = sa.Column('last_review_date', sa.DateTime, nullable=True)
    last_review_uuid = sa.Column('last_review_uuid', sa.String(36), nullable=True)

    last_modified = sa.Column('last_modified', sa.DateTime, nullable=True)
    expires = sa.Column('expires', sa.DateTime, nullable=True)

    violations_count = sa.Column('violations_count', sa.Integer, server_default='0', nullable=False)

    score = sa.Column('score', sa.Float, default=0.0, nullable=False)

    def to_dict(self):
        return {
            'uuid': str(self.uuid),
            'url': self.url,
            'lastModified': self.last_modified,
            'expires': self.expires,
            'score': self.score
        }

    def __str__(self):
        return str(self.uuid)

    def __repr__(self):
        return str(self)

    def get_violations_per_day(self, db):
        from holmes.models import Review, Violation  # Prevent circular dependency

        violations = db \
            .query(
                sa.func.year(Review.completed_date).label('year'),
                sa.func.month(Review.completed_date).label('month'),
                sa.func.day(Review.completed_date).label('day'),
                sa.func.count(Violation.id).label('violation_count'),
                sa.func.sum(Violation.points).label('violation_points')
            ).join(
                Page, Page.id == Review.page_id
            ).join(
                Violation, Violation.review_id == Review.id
            ).filter(Review.is_complete == True).filter(Review.page_id == self.id) \
            .group_by(
                sa.func.year(Review.completed_date),
                sa.func.month(Review.completed_date),
                sa.func.day(Review.completed_date),
            ) \
            .order_by(Review.completed_date) \
            .all()

        result = []

        for day in violations:
            dt = "%d-%d-%d" % (day.year, day.month, day.day)
            result.append({
                "completedAt": dt,
                "violation_count": int(day.violation_count),
                "violation_points": int(day.violation_points)
            })

        return result

    @classmethod
    def by_uuid(cls, uuid, db):
        return db.query(Page).filter(Page.uuid == uuid).first()

    @classmethod
    def by_url_hash(cls, url_hash, db):
        return db.query(Page).filter(Page.url_hash==url_hash).first()

    @classmethod
    def get_page_count(cls, db):
        return int(db.query(sa.func.count(Page.id)).scalar())

    @classmethod
    def update_scores(cls, individual_score, db):
        for i in range(3):
            db.begin(subtransactions=True)
            try:
                db.query(Page).update({'score': Page.score + individual_score})
                db.flush()
                db.commit()
                break
            except Exception:
                err = sys.exc_info()[1]
                if 'Deadlock found' in str(err) or 'Lock wait' in str(err):
                    logging.error('Deadlock happened! Trying again (try number %d)! (Details: %s)' % (i, str(err)))
                else:
                    db.rollback()
                    raise

    @classmethod
    def get_next_job_list(cls, db, expiration, current_page=1, page_size=200):
        from holmes.models import Domain

        lower_bound = (current_page - 1) * page_size
        upper_bound = lower_bound + page_size

        active_domains = Domain.get_active_domains(db)
        active_domains_ids = [item.id for item in active_domains]

        pages_query = db \
            .query(
                Page.uuid,
                Page.url,
                Page.score,
                Page.last_review_date
            ) \
            .filter(Page.domain_id.in_(active_domains_ids)) \
            .order_by(Page.score.desc())

        return pages_query[lower_bound:upper_bound]

    @classmethod
    def get_next_jobs_count(cls, db, config):
        from holmes.models import Domain

        active_domains = Domain.get_active_domains(db)
        active_domains_ids = [item.id for item in active_domains]

        return db \
                .query(
                    sa.func.count(Page.id)
                ) \
                .filter(Page.domain_id.in_(active_domains_ids)) \
                .scalar()

    @classmethod
    def get_next_job(cls, db, expiration, cache, lock_expiration, avg_links_per_page=10):
        from holmes.models import Settings, Worker, Domain, Limiter  # Avoid circular dependency

        page = None
        lock = None
        settings = Settings.instance(db)
        workers = db.query(Worker).all()
        number_of_workers = len(workers)

        active_domains = Domain.get_active_domains(db)
        active_domains_ids = [item.id for item in active_domains]

        all_domains_pages_in_need_of_review = {}

        for domain_id in active_domains_ids:
            pages = db \
                .query(
                    Page.uuid,
                    Page.url,
                    Page.score,
                    Page.last_review_date
                ) \
                .filter(Page.domain_id == domain_id) \
                .order_by(Page.score.desc())[:number_of_workers]
            if pages:
                all_domains_pages_in_need_of_review[domain_id] = pages

        pages_in_need_of_review = []
        current_domain = 0
        while all_domains_pages_in_need_of_review:
            domains = all_domains_pages_in_need_of_review.keys()
            if current_domain >= len(domains):
                current_domain = 0

            domain_id = domains[current_domain]

            item = all_domains_pages_in_need_of_review[domain_id].pop(0)
            pages_in_need_of_review.append(item)

            if not all_domains_pages_in_need_of_review[domain_id]:
                del all_domains_pages_in_need_of_review[domain_id]

            current_domain += 1

        if not pages_in_need_of_review:
            return None

        if settings.lambda_score > 0 and settings.lambda_score > pages_in_need_of_review[0].score:
            cls.update_pages_score_by(settings, settings.lambda_score, db)

        for i in range(len(pages_in_need_of_review)):
            if not Limiter.has_limit_to_work(db, active_domains, pages_in_need_of_review[i].url, avg_links_per_page):
                continue

            lock = cache.has_next_job_lock(
                pages_in_need_of_review[i].url,
                lock_expiration
            )

            if lock is not None:
                page = pages_in_need_of_review[i]
                break

        if page is None:
            return None

        return {
            'page': str(page.uuid),
            'url': page.url,
            'score': page.score,
            'lock': lock
        }

    @classmethod
    def update_pages_score_by(cls, settings, score, db):
        settings.lambda_score = 0
        page_count = cls.get_page_count(db)
        individual_score = float(score) / float(page_count)
        cls.update_scores(individual_score, db)

    @classmethod
    @return_future
    def add_page(cls, db, cache, url, score, fetch_method, publish_method, config, callback):
        domain_name, domain_url = get_domain_from_url(url)
        if not url or not domain_name:
            callback((False, url, {
                'reason': 'invalid_url',
                'url': url,
                'status': None,
                'details': 'Domain name could not be determined.'
            }))
            return

        logging.debug('Obtaining "%s"...' % url)

        fetch_method(
            url,
            cls.handle_request(cls.handle_add_page(db, cache, url, score, publish_method, config, callback)),
            proxy_host=config.HTTP_PROXY_HOST,
            proxy_port=config.HTTP_PROXY_PORT
        )

    @classmethod
    def handle_request(cls, callback):
        def handle(*args, **kw):
            response = args[-1]  # supports (url, response) and just response

            if hasattr(response, 'status_code'):
                status_code = response.status_code
            elif hasattr(response, 'code'):
                status_code = response.code
            else:
                status_code = 400

            if hasattr(response, 'body'):
                text = response.body
            elif hasattr(response, 'text'):
                text = response.text
            else:
                text = 'Empty response.text'

            callback(status_code, text, response.effective_url)

        return handle

    @classmethod
    def handle_add_page(cls, db, cache, url, score, publish_method, config, callback):
        def handle(code, body, effective_url):
            if code > 399:
                callback((False, url, {
                    'reason': 'invalid_url',
                    'url': url,
                    'status': code,
                    'details': body
                }))
                return

            if effective_url != url:
                callback((False, url, {
                    'reason': 'redirect',
                    'url': url,
                    'effectiveUrl': effective_url
                }))
                return

            domain = cls.add_domain(url, db, publish_method, config)
            page_uuid = cls.insert_or_update_page(url, score, domain, db, publish_method, cache)

            callback((True, url, page_uuid))

        return handle

    @classmethod
    def insert_or_update_page(cls, url, score, domain, db, publish_method, cache):
        url = url.encode('utf-8')
        url_hash = hashlib.sha512(url).hexdigest()
        page = Page.by_url_hash(url_hash, db)

        if page:
            for i in range(3):
                db.begin(subtransactions=True)
                try:
                    db.query(Page).filter(Page.id == page.id).update({'score': Page.score + score})
                    db.flush()
                    db.commit()
                    break
                except Exception:
                    err = sys.exc_info()[1]
                    if 'Deadlock found' in str(err) or 'Lock wait' in str(err):
                        logging.error('Deadlock happened! Trying again (try number %d)! (Details: %s)' % (i, str(err)))
                    else:
                        db.rollback()
                        raise

            return page.uuid

        db.begin(subtransactions=True)
        try:
            page = Page(url=url, url_hash=url_hash, domain=domain, score=score)
            db.add(page)
            db.flush()
            db.commit()
            cache.increment_page_count(domain)
            cache.increment_page_count()
            cache.increment_next_jobs_count()
        except Exception:
            db.rollback()
            err = sys.exc_info()[1]
            if 'Duplicate entry' in str(err):
                logging.error('Duplicate entry! (Details: %s)' % str(err))
            else:
                raise

        publish_method(dumps({
            'type': 'new-page',
            'pageUrl': str(url)
        }))

        return page.uuid

    @classmethod
    def add_domain(cls, url, db, publish_method, config):
        from holmes.models import Domain

        domain_name, domain_url = get_domain_from_url(url)

        domains = db.query(Domain).filter(or_(
            Domain.name == domain_name,
            Domain.name == domain_name.rstrip('/'),
            Domain.name == "%s/" % domain_name
        )).all()

        if not domains:
            domain = None
        else:
            domain = domains[0]

        if not domain:
            url_hash = hashlib.sha512(domain_url).hexdigest()
            domain = Domain(url=domain_url, url_hash=url_hash, name=domain_name)
            db.add(domain)
            db.flush()
            db.commit()

            publish_method(dumps({
                'type': 'new-domain',
                'domainUrl': str(domain_url)
            }))

            from holmes.models import Limiter
            connections = config.DEFAULT_NUMBER_OF_CONCURRENT_CONNECTIONS
            Limiter.add_or_update_limiter(db, domain_url, connections)

        return domain
