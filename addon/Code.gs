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

// ============================================================
// Blocked domains — emails where ALL parties are on these domains
// get a simplified card with no link actions.
// ============================================================
var BLOCKED_DOMAINS = [
  'google.com',
  'accounts.google.com',
  'eagle-exports.com.au',
  'eaglexp.com',
  'eaglexp.com.au',
  'eagle-exports.com'
];

/**
 * Extract the domain from a Gmail address string.
 * Handles formats: "Name" <email@domain.com> or plain email@domain.com
 */
function extractDomain(sender) {
  var match = sender.match(/<([^>]+)>/) || [null, sender];
  var addr = match[1].trim().toLowerCase();
  var parts = addr.split('@');
  return parts.length === 2 ? parts[1] : '';
}

/**
 * Check if a domain is in the blocked list.
 */
function isDomainBlocked(domain) {
  if (!domain) return true;
  for (var i = 0; i < BLOCKED_DOMAINS.length; i++) {
    if (domain === BLOCKED_DOMAINS[i]) return true;
  }
  return false;
}

/**
 * Determine the relevant external contact for an email message.
 *
 * For received emails (sender is external) → the sender is the contact.
 * For sent/draft emails (sender is internal) → the first external To
 * recipient is the contact.
 *
 * Returns { address: string, direction: 'received'|'sent' } or null if
 * all parties are blocked/internal.
 */
function getRelevantContact(message) {
  var sender = message.getFrom();
  var senderDomain = extractDomain(sender);

  // If sender is external → relevant contact is the sender
  if (!isDomainBlocked(senderDomain)) {
    return { address: sender, direction: 'received' };
  }

  // Sender is internal — check To recipients for an external address
  var to = message.getTo();
  if (to) {
    var recipients = to.split(',');
    for (var i = 0; i < recipients.length; i++) {
      var recip = recipients[i].trim();
      var recipDomain = extractDomain(recip);
      if (recipDomain && !isDomainBlocked(recipDomain)) {
        return { address: recip, direction: 'sent' };
      }
    }
  }

  // All parties are blocked/internal
  return null;
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
        .setTitle('Eagle Agent')
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

  // Activate temporary Gmail scopes for reading message metadata
  var accessToken = e.gmail.accessToken;
  GmailApp.setCurrentMessageAccessToken(accessToken);

  var messageId = e.gmail.messageId;
  var message = GmailApp.getMessageById(messageId);
  var subject = message.getSubject();
  var sender = message.getFrom();
  var threadId = message.getThread().getId();

  // Determine the relevant external contact (sender for received, To for sent/draft)
  var contact = getRelevantContact(message);
  if (!contact) {
    return [buildNoActionsCard(subject)];
  }

  // Use the external contact address for backend matching
  var contactAddress = contact.address;

  // Call backend for entity linking context
  var context;
  try {
    context = fetchBackend('/api/addon/context', {
      gmail_message_id: messageId,
      gmail_thread_id: threadId,
      subject: subject,
      sender: contactAddress
    });
  } catch (err) {
    return [buildErrorCard(err.message)];
  }

  return [buildContextCard(context, messageId, threadId, subject, contactAddress)];
}

// ============================================================
// UI: Build the context card
// ============================================================

function buildContextCard(context, messageId, threadId, subject, sender) {
  var builder = CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader()
        .setTitle('Eagle Agent')
        .setSubtitle(subject.length > 60 ? subject.substring(0, 57) + '...' : subject)
    );

  // ---- Status section: linked entities ----
  var statusSection = CardService.newCardSection().setHeader('Linked Entities');

  if (context.customer) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Customer')
        .setText(context.customer.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.PERSON)
        )
    );
  }

  if (context.supplier) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Supplier')
        .setText(context.supplier.name)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.STAR)
        )
    );
  }

  if (context.rfq) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('RFQ')
        .setText(context.rfq.rfq_number + ' \u2014 ' + context.rfq.status)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.DESCRIPTION)
        )
    );
  }

  if (context.opportunity) {
    statusSection.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Opportunity')
        .setText(context.opportunity.title)
        .setStartIcon(
          CardService.newIconImage().setIcon(CardService.Icon.BOOKMARK)
        )
    );
  }

  if (
    !context.customer &&
    !context.supplier &&
    !context.rfq &&
    !context.opportunity
  ) {
    statusSection.addWidget(
      CardService.newTextParagraph()
        .setText('<i>No linked entities found for this email.</i>')
    );
  }

  builder.addSection(statusSection);

  // ---- Actions section ----
  var actionsSection = CardService.newCardSection().setHeader('Actions');

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
  }

  // Only show "Create New RFQ" if a customer is already linked
  if (context.customer) {
    actionsSection.addWidget(
      CardService.newTextButton()
        .setText('Create New RFQ')
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
  }

  builder.addSection(actionsSection);

  return builder.build();
}

// ============================================================
// UI: Error card
// ============================================================

function buildErrorCard(message) {
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Eagle Agent')
    )
    .addSection(
      CardService.newCardSection()
        .addWidget(
          CardService.newDecoratedText()
            .setTopLabel('Error')
            .setText(message)
            .setStartIcon(
              CardService.newIconImage().setIcon(CardService.Icon.INVITE)
            )
        )
    )
    .build();
}

// ============================================================
// UI: Fallback card for editor / no-Gmail-context
// ============================================================

function buildEditorFallbackCard() {
  return CardService.newCardBuilder()
    .setHeader(
      CardService.newCardHeader().setTitle('Eagle Agent')
    )
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
        .setTitle('Eagle Agent')
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
      if (data.matched && data.entity) {
        // Found — show suggestion card
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
      }
    }
  } catch (err) {
    // Match failed — fall through to manual type chooser
  }

  // No match — show type chooser
  return showTypeChooser(e.parameters.messageId, e.parameters.threadId, sender, subject);
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
                      rfqToken: rfq.rfq_number
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
