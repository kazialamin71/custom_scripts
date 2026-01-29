# -*- coding: utf-8 -*-
import re
from openerp.osv import osv, fields
from openerp.tools.translate import _


class opd_journal_generator_wizard(osv.osv_memory):
    _name = 'opd.journal.generator.wizard'
    _description = 'Generate OPD Journals From OPD List'

    _columns = {
        'opd_list': fields.text(
            'OPD List',
            required=True,
            help="Paste OPD numbers one per line (e.g. OPD-0412768). Quotes/commas are OK."
        ),
        'only_confirmed': fields.boolean('Only Confirmed'),
        'include_draft': fields.boolean('Treat Draft Journal As Existing'),
        'cash_account_id': fields.many2one('account.account', 'Cash/Bank Debit Account', required=True),
        'journal_id': fields.many2one('account.journal', 'Journal', required=True),
        'commit_every': fields.integer('Commit Every'),
    }

    _defaults = {
        'only_confirmed': True,
        'include_draft': False,
        'cash_account_id': 6,   # your cash/bank account id
        'journal_id': 2,        # your sales journal id
        'commit_every': 100,
    }

    # ---------------------------
    # Helpers
    # ---------------------------
    def _normalize_opd_tokens(self, text):
        """
        Accepts messy input like:
          "OPD-0412768"
          OPD-0412869, OPD-0413086
        Returns list like:
          ['OPD-0412768', 'OPD-0412869', ...]
        """
        if not text:
            return []
        # find OPD- followed by digits (allow spaces around)
        found = re.findall(r'OPD-\s*\d+', text, flags=re.IGNORECASE)
        tokens = []
        for f in found:
            f = f.strip().upper().replace(' ', '')  # OPD- 0412 -> OPD-0412
            tokens.append(f)
        # unique (keep order)
        seen = set()
        out = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _opd_ref_to_ticket_id(self, cr, uid, opd_ref, context=None):
        """
        opd_ref = 'OPD-0412768' (full)
        In your system, ticket.name is this full string.
        So we search by name (fast and safe).
        """
        ticket_obj = self.pool.get('opd.ticket')
        ids = ticket_obj.search(cr, uid, [('name', '=', opd_ref)], limit=1, context=context)
        return ids and ids[0] or False
    def _period_id_for_date(self, cr, uid, dt, context=None):
        """
        Robust period detection for Odoo 8:
        - trims datetime strings to YYYY-MM-DD
        - passes dt as parameter (more reliable than context['dt'])
        """
        ctx = dict(context or {})

        # dt can be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'
        if dt and isinstance(dt, basestring):
            dt = dt[:10]  # keep only YYYY-MM-DD

        if not dt:
            raise osv.except_osv(_('Period Error'), _('Ticket date is empty, cannot find period.'))

        # Prefer normal periods (not special)
        ctx['account_period_prefer_normal'] = True

        period_ids = self.pool.get('account.period').find(cr, uid, dt=dt, context=ctx)
        return period_ids and period_ids[0] or False

    def _move_exists(self, cr, uid, ref, include_draft=False, context=None):
        dom = [('ref', '=', ref)]
        if not include_draft:
            dom.append(('state', '=', 'posted'))
        move_ids = self.pool.get('account.move').search(cr, uid, dom, limit=1, context=context)
        return bool(move_ids)

    def _prepare_move_vals(self, cr, uid, wiz, ticket, context=None):
        total = float(ticket.total or 0.0)
        if total <= 0.0:
            raise osv.except_osv(_('Error'), _('Ticket total is zero: %s') % (ticket.name,))

        if wiz.only_confirmed and ticket.state != 'confirmed':
            raise osv.except_osv(_('Skipped'), _('Ticket is not confirmed: %s') % (ticket.name,))

        period_id = self._period_id_for_date(cr, uid, ticket.date, context=context)
        if not period_id:
            raise osv.except_osv(_('Period Error'), _('No period found for date %s (Ticket %s)') % (ticket.date, ticket.name))

        line_ids = []

        # Debit cash/bank
        line_ids.append((0, 0, {
            'name': ticket.name,
            'account_id': wiz.cash_account_id.id,
            'debit': total,
            'credit': 0.0,
            'partner_id': False,
            'analytic_account_id': False,
            'tax_code_id': False,
            'tax_amount': 0.0,
            'currency_id': False,
            'date_maturity': False,
            'amount_currency': 0.0,
        }))

        # Credit income lines
        added = False
        for l in ticket.opd_ticket_line_id:
            if not (l.name and l.name.accounts_id and l.name.accounts_id.id):
                raise osv.except_osv(
                    _('Configuration Error'),
                    _('Income account not set for item "%s" (Ticket %s). Set accounts_id on opd.ticket.entry.')
                    % (l.name and l.name.name or 'Unknown', ticket.name)
                )

            amt = float(l.total_amount or 0.0)
            if amt <= 0.0:
                continue

            line_ids.append((0, 0, {
                'name': (l.name.name or ticket.name),
                'account_id': l.name.accounts_id.id,
                'debit': 0.0,
                'credit': amt,
                'partner_id': False,
                'analytic_account_id': False,
                'tax_code_id': False,
                'tax_amount': 0.0,
                'currency_id': False,
                'date_maturity': False,
                'amount_currency': 0.0,
            }))
            added = True

        if not added:
            raise osv.except_osv(_('Error'), _('No income lines found: %s') % (ticket.name,))

        return {
            'name': '/',
            'journal_id': wiz.journal_id.id,
            'date': ticket.date,
            'period_id': period_id,
            'ref': ticket.name,
            'line_id': line_ids,
        }

    # ---------------------------
    # Button action
    # ---------------------------
    def action_generate(self, cr, uid, ids, context=None):
        context = context or {}
        wiz = self.browse(cr, uid, ids[0], context=context)

        opd_refs = self._normalize_opd_tokens(wiz.opd_list or '')
        if not opd_refs:
            raise osv.except_osv(_('OPD Journal Generator'), _('No valid OPD numbers found. Example: OPD-0412768'))

        ticket_obj = self.pool.get('opd.ticket')
        move_obj = self.pool.get('account.move')

        created = 0
        skipped_existing = 0
        not_found = []
        errors = []

        for opd_ref in opd_refs:
            try:
                # 1) Find ticket by name
                ticket_id = self._opd_ref_to_ticket_id(cr, uid, opd_ref, context=context)
                if not ticket_id:
                    not_found.append(opd_ref)
                    continue

                ticket = ticket_obj.browse(cr, uid, ticket_id, context=context)

                # 2) Skip if move already exists
                if self._move_exists(cr, uid, ticket.name, include_draft=wiz.include_draft, context=context):
                    skipped_existing += 1
                    continue

                # 3) Build move values (uses ticket.date + auto period)
                move_vals = self._prepare_move_vals(cr, uid, wiz, ticket, context=context)

                # 4) Create + post
                move_id = move_obj.create(cr, uid, move_vals, context=context)
                move_obj.button_validate(cr, uid, [move_id], context=context)

                created += 1
                if wiz.commit_every and created % wiz.commit_every == 0:
                    cr.commit()

            except Exception as e:
                errors.append('%s -> %s' % (opd_ref, str(e)))

        cr.commit()

        msg = (
            "Completed.\n"
            "Input OPDs: %s\n"
            "Created: %s\n"
            "Skipped (already has journal): %s\n"
            "Not found in opd_ticket: %s\n"
            "Errors: %s"
        ) % (len(opd_refs), created, skipped_existing, len(not_found), len(errors))

        if not_found:
            msg += "\n\nNot found (first 20):\n- " + "\n- ".join(not_found[:20])
        if errors:
            msg += "\n\nErrors (first 15):\n- " + "\n- ".join(errors[:15])

        raise osv.except_osv(_('OPD Journal Generator'), msg)
