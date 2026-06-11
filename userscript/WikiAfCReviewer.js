/**
 * WikiAfCReviewer — advisory AI assessment panel for Articles for Creation drafts.
 *
 * Adds a read-only "AI Assessment" panel to Draft-namespace pages. It sends the
 * draft title to a backend service, which fetches the wikitext, verifies sources,
 * and returns a policy-grounded assessment (notability / NPOV / COI). It NEVER
 * edits, accepts, declines, or otherwise acts on any page — a human reviewer does
 * all of that (e.g. via AFCH). This is decision support, not a decision-maker.
 *
 * Clean-room implementation. Not derived from AFCH.
 *
 * Copyright (C) 2026  Ordiopside
 * Licensed under the GNU General Public License v3.0 or later.
 */
/* global mw, $ */
( function () {
	'use strict';

	// --- Configuration -------------------------------------------------------
	// Toolforge backend. Must return CORS headers allowing the en.wikipedia.org
	// origin, and holds the API key server-side (never exposed to the browser).
	var BACKEND_URL = 'https://wikireview.toolforge.org/review';
	var DRAFT_NAMESPACE = 118; // "Draft:" namespace number on enwiki.

	// --- Guard: only run on a real Draft page --------------------------------
	if ( mw.config.get( 'wgNamespaceNumber' ) !== DRAFT_NAMESPACE ) {
		return;
	}
	if ( mw.config.get( 'wgArticleId' ) === 0 ) {
		return; // page doesn't exist (e.g. a redlink) — nothing to review.
	}
	if ( mw.config.get( 'wgAction' ) !== 'view' ) {
		return; // only on the read view, not edit/history/etc.
	}

	var TITLE = mw.config.get( 'wgPageName' ); // e.g. "Draft:Bernard_James"

	// --- Small DOM helper: build elements without innerHTML ------------------
	// Backend output is model-generated; treat it as untrusted text and only
	// ever set it via textContent to avoid any HTML/script injection.
	function el( tag, opts, children ) {
		var node = document.createElement( tag );
		opts = opts || {};
		if ( opts.className ) { node.className = opts.className; }
		if ( opts.text != null ) { node.textContent = opts.text; }
		( children || [] ).forEach( function ( c ) { node.appendChild( c ); } );
		return node;
	}

	function yesNo( bool ) {
		return el( 'td', { text: bool ? '✓' : '✗', className: bool ? 'wafc-yes' : 'wafc-no' } );
	}

	// --- Rendering -----------------------------------------------------------
	function renderLoading( panel ) {
		panel.replaceChildren(
			el( 'p', { className: 'wafc-status', text: 'Assessing draft… (verifying sources may take a moment)' } )
		);
	}

	function renderError( panel, message ) {
		panel.replaceChildren(
			el( 'p', { className: 'wafc-error', text: 'Assessment failed: ' + message } )
		);
	}

	function renderResult( panel, data ) {
		var children = [];

		// Verdict.
		children.push( el( 'p', { className: 'wafc-verdict wafc-verdict-' + data.afc_verdict }, [
			el( 'strong', { text: 'Verdict: ' + data.afc_verdict.toUpperCase() } )
		] ) );
		children.push( el( 'p', { text: data.verdict_rationale } ) );

		// Source-by-source notability table.
		if ( data.sources && data.sources.length ) {
			var head = el( 'tr', {}, [ 'Source', 'Exists', 'Independent', 'Reliable', 'Significant', 'Note' ]
				.map( function ( h ) { return el( 'th', { text: h } ); } ) );
			var rows = data.sources.map( function ( s ) {
				return el( 'tr', {}, [
					el( 'td', { text: s.citation } ),
					yesNo( s.exists ), yesNo( s.independent ),
					yesNo( s.reliable ), yesNo( s.significant_coverage ),
					el( 'td', { text: s.note } )
				] );
			} );
			children.push( el( 'p', { text: 'Meets WP:GNG: ' + ( data.meets_gng ? 'likely' : 'not demonstrated' ) } ) );
			children.push( el( 'table', { className: 'wikitable wafc-sources' }, [ el( 'thead', {}, [ head ] ), el( 'tbody', {}, rows ) ] ) );
		}

		// Flags.
		[ [ 'NPOV flags', data.npov_flags ], [ 'COI flags', data.coi_flags ] ].forEach( function ( pair ) {
			if ( pair[ 1 ] && pair[ 1 ].length ) {
				children.push( el( 'p', {}, [ el( 'strong', { text: pair[ 0 ] + ':' } ) ] ) );
				children.push( el( 'ul', {}, pair[ 1 ].map( function ( f ) { return el( 'li', { text: f } ); } ) ) );
			}
		} );

		// Standing disclaimer.
		children.push( el( 'p', { className: 'wafc-disclaimer',
			text: 'Advisory only — AI-generated and may be wrong. A human reviewer makes the decision; this tool makes no edits.' } ) );

		panel.replaceChildren.apply( panel, children );
	}

	// --- Backend call --------------------------------------------------------
	function runReview( panel ) {
		renderLoading( panel );
		fetch( BACKEND_URL, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify( { title: TITLE } )
		} ).then( function ( resp ) {
			if ( !resp.ok ) { throw new Error( 'HTTP ' + resp.status ); }
			return resp.json();
		} ).then( function ( data ) {
			renderResult( panel, data );
		} ).catch( function ( err ) {
			renderError( panel, err.message );
		} );
	}

	// --- Mount the panel + trigger -------------------------------------------
	function mount() {
		var container = el( 'div', { className: 'wafc-container' } );
		var heading = el( 'h3', { text: 'AI Assessment (advisory)' } );
		var panel = el( 'div', { className: 'wafc-panel' } );
		var button = el( 'button', { className: 'mw-ui-button mw-ui-progressive', text: 'Assess this draft' } );

		button.addEventListener( 'click', function () {
			button.disabled = true;
			runReview( panel );
		} );

		container.appendChild( heading );
		container.appendChild( button );
		container.appendChild( panel );

		// Inject above the article body.
		var content = document.querySelector( '.mw-parser-output' );
		if ( content && content.parentNode ) {
			content.parentNode.insertBefore( container, content );
		}

		// Also add a quick link in the toolbox for discoverability.
		mw.util.addPortletLink( 'p-tb', '#', 'AI-assess draft', 't-wafc', 'Run an advisory AI assessment of this draft' )
			.addEventListener( 'click', function ( e ) { e.preventDefault(); button.click(); } );
	}

	mw.loader.using( [ 'mediawiki.util' ] ).then( function () {
		$( mount );
	} );

}() );
