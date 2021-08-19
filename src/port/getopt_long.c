/*
 * getopt_long() -- long options parser
 *
 * Portions Copyright (c) 1987, 1993, 1994
 * The Regents of the University of California.  All rights reserved.
 *
 * Portions Copyright (c) 2003
 * PostgreSQL Global Development Group
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *	  notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *	  notice, this list of conditions and the following disclaimer in the
 *	  documentation and/or other materials provided with the distribution.
 * 3. Neither the name of the University nor the names of its contributors
 *	  may be used to endorse or promote products derived from this software
 *	  without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 *
 * src/port/getopt_long.c
 */

#include "c.h"

#include "getopt_long.h"

#define BADCH	'?'
#define BADARG	':'
#define EMSG	""


/*
 * getopt_long
 *	Parse argc/argv argument vector, with long options.
 *
 * This implementation does not use optreset.  Instead, we guarantee that
 * it can be restarted on a new argv array after a previous call returned -1,
 * if the caller resets optind to 1 before the first call of the new series.
 * (Internally, this means we must be sure to reset "place" to EMSG before
 * returning -1.)
 */
int
getopt_long(int argc, char *const argv[],
			const char *optstring,
			const struct option *longopts, int *longindex)
{
	static char *place = EMSG;	/* option letter processing */
	char	   *oli;			/* option letter list index */

	if (!*place)
	{							/* update scanning pointer */
		if (optind >= argc)
		{
			place = EMSG;
			return -1;
		}

		place = argv[optind];

		if (place[0] != '-')
		{
			place = EMSG;
			return -1;
		}

		place++;

		if (!*place)
		{
			/* treat "-" as not being an option */
			place = EMSG;
			return -1;
		}

		if (place[0] == '-' && place[1] == '\0')
		{
			/* found "--", treat it as end of options */
			++optind;
			place = EMSG;
			return -1;
		}

		if (place[0] == '-' && place[1])
		{
			/* long option */
			int			i;

			place++;

			size_t		namelen = strcspn(place, "=");
			for (i = 0; longopts[i].name != NULL; i++)
			{
				if (strlen(longopts[i].name) == namelen
					&& strncmp(place, longopts[i].name, namelen) == 0)
				{
					int			has_arg = longopts[i].has_arg;

					if (has_arg != no_argument)
					{
						if (place[namelen] == '=')
							optarg = place + namelen + 1;
						else if (optind < argc - 1 &&
								 has_arg == required_argument)
						{
							optind++;
							optarg = argv[optind];
						}
						else
						{
							if (optstring[0] == ':')
								return BADARG;

							if (opterr && has_arg == required_argument)
								fprintf(stderr,
										"%s: option requires an argument -- %s\n",
										argv[0], place);

							place = EMSG;
							optind++;

							if (has_arg == required_argument)
								return BADCH;
							optarg = NULL;
						}
					}
					else
					{
						optarg = NULL;
						if (place[namelen] != 0)
						{
							/* XXX error? */
						}
					}

					optind++;

					if (longindex)
						*longindex = i;

					place = EMSG;

					if (longopts[i].flag == NULL)
						return longopts[i].val;
					else
					{
						*longopts[i].flag = longopts[i].val;
						return 0;
					}
				}
			}

			if (opterr && optstring[0] != ':')
				fprintf(stderr,
						"%s: illegal option -- %s\n", argv[0], place);
			place = EMSG;
			optind++;
			return BADCH;
		}
	}

	/* short option */
	optopt = (int) *place++;

	oli = strchr(optstring, optopt);
	if (!oli)
	{
		if (!*place)
			++optind;
		if (opterr && *optstring != ':')
			fprintf(stderr,
					"%s: illegal option -- %c\n", argv[0], optopt);
		return BADCH;
	}

	if (oli[1] != ':')
	{							/* don't need argument */
		optarg = NULL;
		if (!*place)
			++optind;
	}
	else
	{							/* need an argument */
		if (*place)				/* no white space */
			optarg = place;
		else if (argc <= ++optind)
		{						/* no arg */
			place = EMSG;
			if (*optstring == ':')
				return BADARG;
			if (opterr)
				fprintf(stderr,
						"%s: option requires an argument -- %c\n",
						argv[0], optopt);
			return BADCH;
		}
		else
			/* white space */
			optarg = argv[optind];
		place = EMSG;
		++optind;
	}
	return optopt;
}
