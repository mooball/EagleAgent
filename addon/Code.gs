// ============================================================
// Eagle Agent — Gmail Workspace Add-on
// ============================================================
// Provides contextual email information and actions in the
// Gmail sidebar. Communicates with the EagleAgent FastAPI backend
// authenticated via OIDC identity tokens.
// ============================================================

// The backend URL is the base of the EagleAgent deployment.
// In a future version this could be fetched from script properties.
const BACKEND_URL = 'https://agent.eaglexp.com.au';

// Context cache TTL in seconds (5 minutes).
// Caching is controlled by a script property — set DEPLOYMENT_MODE = "production"
// in Script Properties to enable.  Test/staging deployments skip the cache.
var CONTEXT_CACHE_TTL = 300;

/**
 * Check if context caching is currently enabled.
 *
 * Production deployments set a Script Property:
 *   PropertiesService → Script Properties → DEPLOYMENT_MODE = "production"
 *
 * Test/staging (head) deployments leave this unset → cache is skipped.
 */
function isCacheEnabled() {
  try {
    var mode = PropertiesService.getScriptProperties().getProperty('DEPLOYMENT_MODE');
    return mode === 'production';
  } catch (e) {
    // PropertiesService may not be ready (e.g. panel reopen race)
    return false;
  }
}

// ============================================================
// Blocked domains — emails where ALL parties are on these domains
// get a simplified card with no link actions.
// ============================================================
// Internal/service domains — used only for "isInternal" UI banner.
// Direction detection uses mailbox-owner comparison, not domain lists.
var INTERNAL_DOMAINS = [
  'google.com',
  'accounts.google.com',
  'eagle-exports.com.au',
  'eaglexp.com',
  'eaglexp.com.au',
  'eagle-exports.com'
];

/**
 * Extract the email address from a Gmail address string.
 * Handles formats: "Name" <email@domain.com> or plain email@domain.com
 */
function extractEmail(addrStr) {
  var match = addrStr.match(/<([^>]+)>/);
  return match ? match[1].trim().toLowerCase() : addrStr.trim().toLowerCase();
}

/**
 * Extract the domain from a Gmail address string.
 */
function extractDomain(addrStr) {
  var email = extractEmail(addrStr);
  var parts = email.split('@');
  return parts.length === 2 ? parts[1] : '';
}

/**
 * Check if ALL parties on an email are internal/service domains.
 * Used only for the UI warning banner — not for direction detection.
 */
function isAllInternal(message) {
  var addresses = [];
  addresses.push(message.getFrom());
  var to = message.getTo();
  if (to) {
    var parts = to.split(',');
    for (var i = 0; i < parts.length; i++) {
      addresses.push(parts[i].trim());
    }
  }
  for (var j = 0; j < addresses.length; j++) {
    var domain = extractDomain(addresses[j]);
    if (!domain) continue;
    var found = false;
    for (var k = 0; k < INTERNAL_DOMAINS.length; k++) {
      if (domain === INTERNAL_DOMAINS[k]) { found = true; break; }
    }
    if (!found) return false;
  }
  return true;
}

/**
 * Determine direction, contact address, and sender/recipient for an email.
 *
 * Uses the mailbox owner (Session.getActiveUser()) to reliably determine
 * direction regardless of domain. This handles:
 * - External emails (customer → me or me → customer)
 * - Internal forwards (colleague → me)
 * - Sent emails viewed in sent folder
 *
 * Returns:
 *   { direction: 'received'|'sent',
 *     contact: email address of the other party,
 *     senderEmail: the FROM address,
 *     recipientEmail: the primary TO address,
 *     userEmail: the mailbox owner }
 */
function getEmailContext(message) {
  var userEmail = Session.getActiveUser().getEmail().toLowerCase();
  var fromRaw = message.getFrom();
  var fromEmail = extractEmail(fromRaw);
  var toRaw = message.getTo() || '';
  var toFirstEmail = toRaw ? extractEmail(toRaw.split(',')[0]) : '';

  var direction;
  var contact;

  if (fromEmail === userEmail) {
    // I sent this email — contact is the first TO recipient
    direction = 'sent';
    contact = toFirstEmail || fromRaw;
  } else {
    // Someone else sent this — contact is the sender
    direction = 'received';
    contact = fromRaw;  // keep full "Name <email>" for display/matching
  }

  return {
    direction: direction,
    contact: contact,
    senderEmail: fromEmail,
    recipientEmail: toFirstEmail,
    userEmail: userEmail
  };
}

/**
 * Return a human-readable label for pipeline classification codes.
 */
function pipelineClassificationLabel(classification) {
  var labels = {
    'quote_response': 'Quote Response',
    'clarification_required': 'Clarification Required',
    'declined': 'Declined',
    'acknowledgement': 'Acknowledgement',
    'not_quote': 'Not a Quote',
    'needs_review': 'Needs Review',
  };
  return labels[classification] || classification;
}

// ============================================================
// Helper: Authenticated fetch to backend
// ============================================================

/**
 * POST JSON to the addon API endpoint with an OIDC identity token.
 *
 * ScriptApp.getIdentityToken() requires that:
 *   1. The Apps Script project is linked to a GCP project (done).
 *   2. The 'openid' scope is in appsscript.json (done).
 *   3. The OAuth consent screen has openid scope (done).
 *
 * The token is an OIDC JWT with claims: iss, aud, sub, hd, email,
 * email_verified, name, iat, exp.
 *
 * The backend verifies: signature, issuer (accounts.google.com),
 * expiry, and hd == "eagle-exports.com.au".
 */
function fetchBackend(path, payload) {
  var idToken = ScriptApp.getIdentityToken();
  if (!idToken) {
    throw new Error(
      'Unable to get identity token. Ensure openid scope is granted ' +
      'and the script is linked to a GCP project.'
    );
  }

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + idToken
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(BACKEND_URL + path, options);
  var code = response.getResponseCode();

  if (code === 401) {
    throw new Error('Authentication failed. Contact admin.');
  }
  if (code === 403) {
    throw new Error('Access denied — only eagle-exports.com users.');
  }
  if (code >= 400) {
    throw new Error(
      'Backend error (' + code + '): ' + response.getContentText()
    );
  }

  return JSON.parse(response.getContentText());
}

// ============================================================
// Helper: Refresh context card and pop to root
// ============================================================

/**
 * After a successful link action, re-fetch context from the backend
 * and replace the root card so the user sees updated linked entities.
 */
function refreshAndReturn(messageId, threadId, subject, sender, successMessage) {
  // Clear cached context — linking changes the result
  if (isCacheEnabled() && messageId) {
    try {
      CacheService.getUserCache().remove('ctx:' + messageId);
    } catch (e) {}
  }

  try {
    var context = fetchBackend('/api/addon/context', {
      gmail_message_id: messageId,
      gmail_thread_id: threadId,
      subject: subject || '',
      sender: sender || ''
    });
    var freshCard = buildContextCard(context, messageId, threadId, subject || '', sender || '');
    var nav = CardService.newNavigation().popToRoot().updateCard(freshCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .setNotification(CardService.newNotification().setText(successMessage))
      .build();
  } catch (err) {
    // Refresh failed — pop to (stale) root with success notification
    var nav = CardService.newNavigation().popToRoot();
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .setNotification(CardService.newNotification().setText(successMessage))
      .build();
  }
}

// ============================================================
// Trigger: Homepage (no message context)
// ============================================================
// Called when the user opens the add-on from the Gmail sidebar
// without having a specific message selected.

function onHomepage(e) {
  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setSubtitle('Open an email to see context')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              'Select an email to view linked customers, ' +
              'suppliers, RFQs, and opportunities.'
            )
        )
    )
    .build();

  return [card];
}

// ============================================================
// Trigger: Message opened — fetch context from backend
// ============================================================
// Called when the user opens a Gmail message while the add-on
// is open. The event object provides the message ID and an
// access token for reading message metadata.

function onGmailMessageOpen(e) {
  // Guard: if running from editor (no event), return a fallback card
  if (!e || !e.gmail) {
    return [buildEditorFallbackCard()];
  }

  try {
    // Activate temporary Gmail scopes for reading message metadata
    var accessToken = e.gmail.accessToken;
    GmailApp.setCurrentMessageAccessToken(accessToken);

    var messageId = e.gmail.messageId;
    var message = GmailApp.getMessageById(messageId);
    var subject = message.getSubject();
    var sender = message.getFrom();
    var threadId = message.getThread().getId();

    // Determine direction and relevant contact using mailbox owner
    var emailCtx = getEmailContext(message);
    var isInternal = isAllInternal(message);
    var contactAddress = emailCtx.contact;

    // Check cache first (production only — skipped when DEPLOYMENT_MODE not set)
    var cacheKey = 'ctx:' + messageId;
    if (isCacheEnabled()) {
      var cache = CacheService.getUserCache();
      var cached = cache.get(cacheKey);
      if (cached) {
        return [buildContextCard(JSON.parse(cached), messageId, threadId, subject, contactAddress, isInternal)];
      }
    }

    // Call backend for entity linking context
    var context;
    try {
      context = fetchBackend('/api/addon/context', {
        gmail_message_id: messageId,
        gmail_thread_id: threadId,
        subject: subject,
        sender: contactAddress,
        direction: emailCtx.direction,
        sender_email: emailCtx.senderEmail,
        recipient_email: emailCtx.recipientEmail,
        user_email: emailCtx.userEmail
      });
    } catch (err) {
      return [buildErrorCard(err.message)];
    }

    // Populate cache (production only)
    if (isCacheEnabled()) {
      cache.put(cacheKey, JSON.stringify(context), CONTEXT_CACHE_TTL);
    }

    return [buildContextCard(context, messageId, threadId, subject, contactAddress, isInternal)];
  } catch (e) {
    return [buildErrorCard(e.message || String(e), e.stack || '')];
  }
}

// ============================================================
// UI: Build the context card
// ============================================================

function buildContextCard(context, messageId, threadId, subject, sender, isInternal) {
  isInternal = isInternal || false;
  var builder = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setSubtitle(subject.length > 60 ? subject.substring(0, 57) + '...' : subject)
    );

  // ---- Internal email warning ----
  if (isInternal) {
    builder.addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              '<b>⚠ All parties on this email are internal or service domains.</b>'
            )
        )
    );
  }

  // ---- Pipeline status (supplier quote classification) ----
  if (context.pipeline_status) {
    var label = pipelineClassificationLabel(context.pipeline_status.classification);
    builder.addSection(
      CardService.newCardSection()
        .setHeader('Email Classification')
        .addWidget(
          CardService.newDecoratedText()
            .setText(label)
            .setBottomLabel(context.pipeline_status.reason || '')
            .setWrapText(true)
        )
    );
  }

  // ---- Linked entity sections (one per type) ----

  if (context.customer) {
    var custSection = CardService.newCardSection().setHeader('Customer');
    custSection.addWidget(
      CardService.newDecoratedText()
        .setText(context.customer.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.PERSON)
        )
    );
    builder.addSection(custSection);
  }

  if (context.supplier) {
    var suppSection = CardService.newCardSection().setHeader('Supplier');
    suppSection.addWidget(
      CardService.newDecoratedText()
        .setText(context.supplier.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.STAR)
        )
    );
    builder.addSection(suppSection);
  }

  if (context.rfq) {
    var rfqSection = CardService.newCardSection().setHeader('RFQ');
    rfqSection.addWidget(
      CardService.newDecoratedText()
        .setText(context.rfq.rfq_number + ' \u2014 ' + context.rfq.status)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.DESCRIPTION)
        )
    );
    rfqSection.addWidget(
      CardService.newTextButton()
        .setText('View in Dashboard')
        .setOpenLink(
          CardService.newOpenLink()
            .setUrl(BACKEND_URL + '/rfqs/' + context.rfq.rfq_number)
        )
    );
    builder.addSection(rfqSection);
  }

  if (context.opportunity) {
    var oppSection = CardService.newCardSection().setHeader('Opportunity');
    oppSection.addWidget(
      CardService.newDecoratedText()
        .setText(context.opportunity.title)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.BOOKMARK)
        )
    );
    builder.addSection(oppSection);
  }

  if (
    !context.customer &&
    !context.supplier &&
    !context.rfq &&
    !context.opportunity
  ) {
    var emptySection = CardService.newCardSection();
    emptySection.addWidget(
      CardService.newTextParagraph()
        .setText('<i>No linked entities found for this email.</i>')
    );
    builder.addSection(emptySection);
  }

  // ---- Actions section ----
  var actionsSection = CardService.newCardSection().setHeader('Actions');
  var hasActions = false;

  // Only show "Link to Customer/Supplier" if neither is already linked
  if (!context.customer && !context.supplier) {
    actionsSection.addWidget(
      CardService.newTextButton()
        .setText('Link to Customer / Supplier')
        .setOnClickAction(
          CardService.newAction()
            .setFunctionName('onLinkEntity')
            .setParameters({
              messageId: messageId,
              threadId: threadId,
              sender: sender,
              subject: subject
            })
        )
    );
    hasActions = true;
  }

  // Only show "Link to RFQ" if not already linked
  if (!context.rfq) {
    actionsSection.addWidget(
      CardService.newTextButton()
        .setText('Link to RFQ')
        .setOnClickAction(
          CardService.newAction()
            .setFunctionName('onLinkRfq')
            .setParameters({
              messageId: messageId,
              threadId: threadId,
              subject: subject,
              sender: sender
            })
        )
    );
    hasActions = true;
  }

  // Only show "Create RFQ + OP" if a customer is linked and no RFQ exists yet
  if (context.customer && !context.rfq) {
    actionsSection.addWidget(
      CardService.newTextButton()
        .setText('Create RFQ + OP')
        .setOnClickAction(
          CardService.newAction()
            .setFunctionName('onCreateRfq')
            .setParameters({
              messageId: messageId,
              threadId: threadId,
              subject: subject,
              sender: sender
            })
        )
    );
    hasActions = true;
  }

  // Show "Manage Links" if any entity is already linked
  if (context.customer || context.supplier || context.rfq) {
    actionsSection.addWidget(
      CardService.newTextButton()
        .setText('Manage Links')
        .setOnClickAction(
          CardService.newAction()
            .setFunctionName('onManageLinks')
            .setParameters({
              messageId: messageId,
              threadId: threadId,
              sender: sender,
              subject: subject,
              customer: context.customer ? JSON.stringify(context.customer) : '',
              supplier: context.supplier ? JSON.stringify(context.supplier) : '',
              rfq: context.rfq ? JSON.stringify(context.rfq) : ''
            })
        )
    );
    hasActions = true;
  }

  if (hasActions) {
    builder.addSection(actionsSection);
  }

  return builder.build();
}

// ============================================================
// UI: Error card
// ============================================================

function buildErrorCard(message, stack) {
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Error')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText('<b>Error:</b><br><br>' + _escapeHtml(message))
        )
    )
    .addSection(
      CardService.newCardSection()
        .setHeader('Details')
        .addWidget(
          CardService.newTextParagraph()
            .setText('<font size="-2" color="#999"><pre>' + _escapeHtml(stack || message) + '</pre></font>')
        )
    )
    .build();
}

function _escapeHtml(str) {
  return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ============================================================
// UI: Fallback card for editor / no-Gmail-context
// ============================================================

function buildEditorFallbackCard() {
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader())
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              'This add-on works inside Gmail. <b>Open an email</b> ' +
              'in Gmail to see linked customers, suppliers, RFQs, ' +
              'and opportunities.'
            )
        )
    )
    .build();
}

// ============================================================
// UI: No-actions card (for blacklisted / service domains)
// ============================================================

function buildNoActionsCard(subject) {
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setSubtitle(
          subject.length > 60 ? subject.substring(0, 57) + '...' : subject
        )
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              '<i>All parties on this email are internal or service ' +
              'domains. No linking actions are needed.</i>'
            )
        )
    )
    .build();
}

// ============================================================
// Phase 2: Link to Customer / Supplier — match-first flow
// ============================================================

/**
 * Step 1: User clicks "Link to Customer / Supplier".
 * Call the match endpoint first.  If a match is found, show a
 * suggestion card.  Otherwise, fall through to the type chooser.
 */
function onLinkEntity(e) {
  var sender = e.parameters.sender;
  var subject = e.parameters.subject;

  // Call the match endpoint
  var matchUrl = BACKEND_URL + '/api/addon/match';
  var idToken = ScriptApp.getIdentityToken();

  try {
    var response = UrlFetchApp.fetch(matchUrl, {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + idToken },
      payload: JSON.stringify({ sender: sender }),
      muteHttpExceptions: true
    });

    if (response.getResponseCode() === 200) {
      var data = JSON.parse(response.getContentText());
      if (data.matched) {
        if (data.is_unique && data.entity) {
          // Single unique match — show suggestion card
          var matchCard = buildMatchSuggestionCard(
            data.entity,
            e.parameters.messageId,
            e.parameters.threadId,
            sender,
            subject
          );
          var nav = CardService.newNavigation().pushCard(matchCard);
          return CardService.newActionResponseBuilder()
            .setNavigation(nav)
            .build();
        } else if (!data.is_unique && data.candidates && data.candidates.length > 1) {
          // Multiple candidates — show picker card
          var pickerCard = buildCandidatePickerCard(
            data.candidates,
            e.parameters.messageId,
            e.parameters.threadId,
            sender,
            subject
          );
          var nav = CardService.newNavigation().pushCard(pickerCard);
          return CardService.newActionResponseBuilder()
            .setNavigation(nav)
            .build();
        }
      }
    }
  } catch (err) {
    // Match failed — fall through to manual type chooser
  }

  // No match — show type chooser
  return showTypeChooser(e.parameters.messageId, e.parameters.threadId, sender, subject);
}


/**
 * Build a picker card showing multiple candidate matches for the user to choose.
 */
function buildCandidatePickerCard(candidates, messageId, threadId, sender, subject) {
  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Multiple Matches Found')
        .setSubtitle(candidates.length + ' entities share this ' +
          (candidates[0].match_type === 'exact' ? 'email' : 'domain'))
    );

  var section = CardService.newCardSection()
    .addWidget(
      CardService.newTextParagraph()
        .setText(
          'This sender matches <b>' + candidates.length + ' entities</b>. ' +
          'Please select the correct one to link.'
        )
    );

  // List each candidate with a "Link" button
  for (var i = 0; i < candidates.length; i++) {
    var c = candidates[i];
    var label = c.type === 'customer' ? 'Customer' : 'Supplier';
    var badge = c.type === 'customer' ? '🏢' : '🏭';
    var via = c.match_type === 'exact' ? 'exact email' : 'domain';

    section.addWidget(
      CardService.newDecoratedText()
        .setTopLabel(label + ' · ' + via)
        .setText(badge + ' ' + c.name)
        .setBottomLabel('Click to link this email to ' + c.name)
        .setOnClickAction(
          CardService.newAction()
            .setFunctionName('onConfirmLink')
            .setParameters({
              messageId: messageId,
              threadId: threadId,
              linkType: c.type,
              entityId: c.id,
              entityName: c.name,
              sender: sender,
              subject: subject
            })
        )
    );
  }

  // Also offer manual search as fallback
  section.addWidget(
    CardService.newTextButton()
      .setText('None of these — search manually')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('onManualLink')
          .setParameters({
            messageId: messageId,
            threadId: threadId,
            sender: sender,
            subject: subject
          })
      )
  );

  card.addSection(section);
  return card.build();
}

/**
 * Build the "we found a match" suggestion card.
 */
function buildMatchSuggestionCard(entity, messageId, threadId, sender, subject) {
  var label = entity.type === 'customer' ? 'Customer' : 'Supplier';
  var via = entity.match_type === 'exact'
    ? 'exact email match'
    : 'domain match';

  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle(entity.name)
        .setSubtitle(label + ' — ' + via)
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextParagraph()
            .setText(
              'We matched this sender to <b>' + entity.name +
              '</b> (' + label.toLowerCase() + '), based on ' + via + '.'
            )
        )
        .addWidget(
          CardService.newTextButton()
            .setText('Confirm — Link to ' + entity.name)
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onConfirmLink')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  linkType: entity.type,
                  entityId: entity.id,
                  entityName: entity.name,
                  sender: sender,
                  subject: subject
                })
            )
        )
        .addWidget(
          CardService.newTextButton()
            .setText('No, search manually')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onManualLink')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  sender: sender,
                  subject: subject
                })
            )
        )
    )
    .build();

  return card;
}

/**
 * User clicked "Confirm Link" — call the link-email endpoint.
 */
function onConfirmLink(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/link-email', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId,
      link_type: e.parameters.linkType,
      entity_id: e.parameters.entityId,
      sender: e.parameters.sender,
      save_domain: true
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  return refreshAndReturn(
    e.parameters.messageId,
    e.parameters.threadId,
    e.parameters.subject,
    e.parameters.sender,
    'Linked to ' + result.entity_name
  );
}

/**
 * User clicked "No, search manually" — show the type chooser.
 */
function onManualLink(e) {
  return showTypeChooser(e.parameters.messageId, e.parameters.threadId, e.parameters.sender, e.parameters.subject);
}

/**
 * Show the type chooser card (Customer or Supplier).
 */
function showTypeChooser(messageId, threadId, sender, subject) {
  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Link to Customer or Supplier?')
        .setSubtitle('Choose the entity type')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextButton()
            .setText('Customer')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onChooseEntityType')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  sender: sender,
                  subject: subject,
                  linkType: 'customer'
                })
            )
        )
        .addWidget(
          CardService.newTextButton()
            .setText('Supplier')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onChooseEntityType')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  sender: sender,
                  subject: subject,
                  linkType: 'supplier'
                })
            )
        )
    )
    .build();

  var nav = CardService.newNavigation().pushCard(card);
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .build();
}

/**
 * Step 2: User chose Customer or Supplier. Push a search card.
 */
function onChooseEntityType(e) {
  var linkType = e.parameters.linkType;
  var label = linkType === 'customer' ? 'Customer' : 'Supplier';

  var searchInput = CardService.newTextInput()
    .setFieldName('searchQuery')
    .setTitle('Search ' + label + 's')
    .setHint('Type a name and press Search');

  var searchButton = CardService.newTextButton()
    .setText('Search')
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName('onSearchEntity')
        .setParameters({
          messageId: e.parameters.messageId,
          threadId: e.parameters.threadId,
          linkType: linkType,
          sender: e.parameters.sender,
          subject: e.parameters.subject
        })
    );

  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Link to ' + label)
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(searchInput)
        .addWidget(searchButton)
    )
    .addSection(
      CardService.newCardSection()
        .setHeader('Results')
        .addWidget(
          CardService.newTextParagraph()
            .setText('<i>Type a name and press Search.</i>')
        )
    )
    .build();

  var nav = CardService.newNavigation().pushCard(card);
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .build();
}

/**
 * Step 3: User typed in the search box. Call the backend and rebuild the
 * results section of the current card.
 */
function onSearchEntity(e) {
  var query = e.formInput.searchQuery || '';
  var linkType = e.parameters.linkType;
  var sender = e.parameters.sender || '';
  var subject = e.parameters.subject || '';

  if (query.length < 2) {
    // Not enough characters — show prompt
    var emptyCard = CardService.newCardBuilder()
      .setHeader(
        CardService.newCardHeader().setTitle(
          'Link to ' + (linkType === 'customer' ? 'Customer' : 'Supplier')
        )
      )
      .addSection(
        CardService.newCardSection()
          .addWidget(
            CardService.newTextInput()
              .setFieldName('searchQuery')
              .setTitle(
                'Search ' +
                (linkType === 'customer' ? 'Customer' : 'Supplier') +
                's'
              )
              .setHint('Type at least 2 characters')
          )
          .addWidget(
            CardService.newTextButton()
              .setText('Search')
              .setOnClickAction(
                CardService.newAction()
                  .setFunctionName('onSearchEntity')
                  .setParameters({
                    messageId: e.parameters.messageId,
                    threadId: e.parameters.threadId,
                    linkType: linkType,
                    sender: sender,
                    subject: subject
                  })
              )
          )
      )
      .addSection(
        CardService.newCardSection()
          .setHeader('Results')
          .addWidget(
            CardService.newTextParagraph()
              .setText('<i>Type at least 2 characters and press Search.</i>')
          )
      )
      .build();

    var nav = CardService.newNavigation().updateCard(emptyCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();
  }

  // Call backend search
  var url = BACKEND_URL + '/api/addon/search?type=' +
            encodeURIComponent(linkType) + '&q=' + encodeURIComponent(query);
  var idToken = ScriptApp.getIdentityToken();

  try {
    var response = UrlFetchApp.fetch(url, {
      method: 'get',
      headers: { Authorization: 'Bearer ' + idToken },
      muteHttpExceptions: true
    });

    if (response.getResponseCode() !== 200) {
      throw new Error('Search failed: ' + response.getContentText());
    }

    var data = JSON.parse(response.getContentText());
    var results = data.results || [];

    // Build results card
    var label = linkType === 'customer' ? 'Customer' : 'Supplier';
    var resultsCard = CardService.newCardBuilder()
      .setHeader(
        CardService.newCardHeader().setTitle('Link to ' + label)
      );

    var inputSection = CardService.newCardSection()
      .addWidget(
        CardService.newTextInput()
          .setFieldName('searchQuery')
          .setTitle('Search ' + label + 's')
          .setValue(query)
      )
      .addWidget(
        CardService.newTextButton()
          .setText('Search')
          .setOnClickAction(
            CardService.newAction()
              .setFunctionName('onSearchEntity')
              .setParameters({
                messageId: e.parameters.messageId,
                threadId: e.parameters.threadId,
                linkType: linkType,
                sender: sender,
                subject: subject
              })
          )
      );
    resultsCard.addSection(inputSection);

    var resultSection = CardService.newCardSection().setHeader('Results');

    if (results.length === 0) {
      resultSection.addWidget(
        CardService.newTextParagraph()
          .setText('<i>No ' + label + 's found matching "' + query + '".</i>')
      );
    } else {
      for (var i = 0; i < results.length; i++) {
        var entity = results[i];
        resultSection.addWidget(
          CardService.newDecoratedText()
            .setText(entity.name)
            .setWrapText(true)
            .setButton(
              CardService.newTextButton()
                .setText('Link')
                .setOnClickAction(
                  CardService.newAction()
                    .setFunctionName('onSelectEntity')
                    .setParameters({
                      messageId: e.parameters.messageId,
                      threadId: e.parameters.threadId,
                      linkType: linkType,
                      entityId: entity.id,
                      entityName: entity.name,
                      sender: sender,
                      subject: subject
                    })
                )
            )
        );
      }
    }

    resultsCard.addSection(resultSection);

    var nav = CardService.newNavigation().updateCard(resultsCard.build());
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();

  } catch (err) {
    var errCard = CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Search Error'))
      .addSection(
        CardService.newCardSection()
          .addWidget(
            CardService.newTextParagraph()
              .setText('<font color="#d93025">' + err.message + '</font>')
          )
      )
      .build();

    var nav = CardService.newNavigation().updateCard(errCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();
  }
}

/**
 * Step 4: User selected an entity. Call backend to link, show success, pop back.
 */
function onSelectEntity(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/link-email', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId,
      link_type: e.parameters.linkType,
      entity_id: e.parameters.entityId,
      sender: e.parameters.sender || '',
      save_domain: true
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  return refreshAndReturn(
    e.parameters.messageId,
    e.parameters.threadId,
    e.parameters.subject,
    e.parameters.sender,
    'Linked to ' + result.entity_name
  );
}

// ============================================================
// Phase 2: Link to RFQ — search + select flow
// ============================================================

/**
 * User clicked "Create RFQ + OP". Backend creates the RFQ synchronously
 * and returns updated context — use it to auto-refresh the card.
 */
function onCreateRfq(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/create-rfq', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  if (result.status === 'error') {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText(result.message || 'Failed to create RFQ')
      )
      .build();
  }

  // Clear cache so next card open gets fresh data
  if (isCacheEnabled()) {
    CacheService.getUserCache().remove('ctx:' + e.parameters.messageId);
  }

  // Build refreshed card from the context returned by the backend
  var card = buildContextCard(
    result.context,
    e.parameters.messageId,
    e.parameters.threadId,
    e.parameters.subject,
    e.parameters.sender,
    false
  );

  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().pushCard(card))
    .setNotification(
      CardService.newNotification().setText(result.message || 'RFQ + OP created!')
    )
    .build();
}

/**
 * User clicked "Link to RFQ". Push a search card.
 */
function onLinkRfq(e) {
  var searchInput = CardService.newTextInput()
    .setFieldName('searchQuery')
    .setTitle('Search RFQs')
    .setHint('RFQ number, OP number, or customer name');

  var searchButton = CardService.newTextButton()
    .setText('Search')
    .setOnClickAction(
      CardService.newAction()
        .setFunctionName('onSearchRfq')
        .setParameters({
          messageId: e.parameters.messageId,
          threadId: e.parameters.threadId,
          sender: e.parameters.sender,
          subject: e.parameters.subject
        })
    );

  var card = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Link to RFQ')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(searchInput)
        .addWidget(searchButton)
    )
    .addSection(
      CardService.newCardSection()
        .setHeader('Results')
        .addWidget(
          CardService.newTextParagraph()
            .setText('<i>Type a query and press Search.</i>')
        )
    )
    .build();

  var nav = CardService.newNavigation().pushCard(card);
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .build();
}

/**
 * User typed in the RFQ search box. Call the search endpoint and rebuild.
 */
function onSearchRfq(e) {
  var query = e.formInput.searchQuery || '';
  var sender = e.parameters.sender || '';
  var subject = e.parameters.subject || '';

  if (query.length < 2) {
    var emptyCard = buildRfqSearchCard(
      '<i>Type at least 2 characters to search...</i>',
      query,
      e.parameters.messageId,
      e.parameters.threadId,
      sender,
      subject
    );
    var nav = CardService.newNavigation().updateCard(emptyCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();
  }

  var url = BACKEND_URL + '/api/addon/search?type=rfq&q=' +
            encodeURIComponent(query);
  var idToken = ScriptApp.getIdentityToken();

  try {
    var response = UrlFetchApp.fetch(url, {
      method: 'get',
      headers: { Authorization: 'Bearer ' + idToken },
      muteHttpExceptions: true
    });

    if (response.getResponseCode() !== 200) {
      throw new Error('Search failed: ' + response.getContentText());
    }

    var data = JSON.parse(response.getContentText());
    var results = data.results || [];

    var resultsCard = CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Link to RFQ'));

    var inputSection = CardService.newCardSection()
      .addWidget(
        CardService.newTextInput()
          .setFieldName('searchQuery')
          .setTitle('Search RFQs')
          .setValue(query)
      )
      .addWidget(
        CardService.newTextButton()
          .setText('Search')
          .setOnClickAction(
            CardService.newAction()
              .setFunctionName('onSearchRfq')
              .setParameters({
                messageId: e.parameters.messageId,
                threadId: e.parameters.threadId,
                sender: sender,
                subject: subject
              })
          )
      );
    resultsCard.addSection(inputSection);

    var resultSection = CardService.newCardSection().setHeader('Results');

    if (results.length === 0) {
      resultSection.addWidget(
        CardService.newTextParagraph()
          .setText('<i>No RFQs found matching "' + query + '".</i>')
      );
    } else {
      for (var i = 0; i < results.length; i++) {
        var rfq = results[i];
        var line = rfq.rfq_number + ' — ' + rfq.customer;
        if (rfq.opportunity_id) {
          line += ' (' + rfq.opportunity_id + ')';
        }
        resultSection.addWidget(
          CardService.newDecoratedText()
            .setText(line)
            .setBottomLabel(rfq.status)
            .setWrapText(true)
            .setButton(
              CardService.newTextButton()
                .setText('Link')
                .setOnClickAction(
                  CardService.newAction()
                    .setFunctionName('onSelectRfq')
                    .setParameters({
                      messageId: e.parameters.messageId,
                      threadId: e.parameters.threadId,
                      rfqToken: rfq.rfq_number,
                      sender: sender,
                      subject: subject
                    })
                )
            )
        );
      }
    }

    resultsCard.addSection(resultSection);

    var nav = CardService.newNavigation().updateCard(resultsCard.build());
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();

  } catch (err) {
    var errCard = CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Search Error'))
      .addSection(
        CardService.newCardSection()
          .addWidget(
            CardService.newTextParagraph()
              .setText('<font color="#d93025">' + err.message + '</font>')
          )
      )
      .build();

    var nav = CardService.newNavigation().updateCard(errCard);
    return CardService.newActionResponseBuilder()
      .setNavigation(nav)
      .build();
  }
}

/**
 * Build a simple RFQ search card (used for empty/minimal state).
 */
function buildRfqSearchCard(message, query, messageId, threadId, sender, subject) {
  var card = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Link to RFQ'))
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newTextInput()
            .setFieldName('searchQuery')
            .setTitle('Search RFQs')
            .setValue(query || '')
        )
        .addWidget(
          CardService.newTextButton()
            .setText('Search')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onSearchRfq')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  sender: sender || '',
                  subject: subject || ''
                })
            )
        )
    )
    .addSection(
      CardService.newCardSection()
        .setHeader('Results')
        .addWidget(
          CardService.newTextParagraph().setText(message)
        )
    )
    .build();

  return card;
}

/**
 * User selected an RFQ from the search results. Link the email.
 */
function onSelectRfq(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/link-email', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId,
      link_type: 'rfq',
      rfq_token: e.parameters.rfqToken
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  return refreshAndReturn(
    e.parameters.messageId,
    e.parameters.threadId,
    e.parameters.subject,
    e.parameters.sender,
    'Linked to ' + result.entity_name
  );
}

// ============================================================
// Phase 2: Unlink from entity
// ============================================================

/**
 * User clicked "Unlink" on a linked entity.  Call the unlink endpoint
 * and refresh the context card.
 */
function onUnlinkEntity(e) {
  var result;
  try {
    result = fetchBackend('/api/addon/unlink', {
      gmail_message_id: e.parameters.messageId,
      gmail_thread_id: e.parameters.threadId,
      link_type: e.parameters.linkType
    });
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(
        CardService.newNotification().setText('Error: ' + err.message)
      )
      .build();
  }

  return refreshAndReturn(
    e.parameters.messageId,
    e.parameters.threadId,
    e.parameters.subject,
    e.parameters.sender,
    result.message || 'Unlinked'
  );
}

// ============================================================
// Phase 2: Manage Links card
// ============================================================

/**
 * User clicked "Manage Links" — show a card listing all linked
 * entities with unlink buttons.  Two-click unlink flow keeps
 * the main UI clean.
 */
function onManageLinks(e) {
  var customer = e.parameters.customer ? JSON.parse(e.parameters.customer) : null;
  var supplier = e.parameters.supplier ? JSON.parse(e.parameters.supplier) : null;
  var rfq = e.parameters.rfq ? JSON.parse(e.parameters.rfq) : null;

  var card = buildUnlinkCard(
    e.parameters.messageId,
    e.parameters.threadId,
    e.parameters.sender,
    e.parameters.subject,
    customer,
    supplier,
    rfq
  );

  var nav = CardService.newNavigation().pushCard(card);
  return CardService.newActionResponseBuilder()
    .setNavigation(nav)
    .build();
}

/**
 * Build a card showing current links with per-entity unlink buttons.
 */
function buildUnlinkCard(messageId, threadId, sender, subject, customer, supplier, rfq) {
  var builder = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Manage Links')
    );

  var section = CardService.newCardSection();

  if (customer) {
    section.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Customer')
        .setText(customer.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.PERSON)
        )
        .setButton(
          CardService.newTextButton()
            .setText('Unlink')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onUnlinkEntity')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  linkType: 'customer',
                  sender: sender,
                  subject: subject
                })
            )
        )
    );
  }

  if (supplier) {
    section.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Supplier')
        .setText(supplier.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.STAR)
        )
        .setButton(
          CardService.newTextButton()
            .setText('Unlink')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onUnlinkEntity')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  linkType: 'supplier',
                  sender: sender,
                  subject: subject
                })
            )
        )
    );
  }

  if (rfq) {
    section.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('RFQ')
        .setText(rfq.rfq_number + ' \u2014 ' + (rfq.status || 'draft'))
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.DESCRIPTION)
        )
        .setButton(
          CardService.newTextButton()
            .setText('Unlink')
            .setOnClickAction(
              CardService.newAction()
                .setFunctionName('onUnlinkEntity')
                .setParameters({
                  messageId: messageId,
                  threadId: threadId,
                  linkType: 'rfq',
                  sender: sender,
                  subject: subject
                })
            )
        )
    );
  }

  builder.addSection(section);
  return builder.build();
}
